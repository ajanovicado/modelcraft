"""Layout operations on the committed fixtures: what they write, what they refuse, and the
references they keep valid.

The twin (``structure-twin.pbix``) is one full 1920 x 1200 page; ``multipage.pbix`` adds a page
with nested groups, a bookmark over them, interactions, an image and a button, plus a drillthrough
and a tooltip page. Positions in operations are page pixels: a member of a group is stored
relative to its group, so these tests read boxes through ``page_box`` and stored values through the
visual's own ``position``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import (
    report_edit_align,
    report_edit_geometry,
    report_edit_layout,
    report_edits,
    report_roles,
)
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PageDoc, PbirDocument
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import (
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    VisibleIn,
)
from ai_powerbi_analyst.studio.report_edit_geometry import leaf_boxes, occupied, page_box
from ai_powerbi_analyst.studio.report_refs import ReferenceIndex
from ai_powerbi_analyst.studio.report_roles import Box, overlaps

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"

PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"
TOOLTIP = "ReportSectionb7e9f1a3c5d7e9f1a3b5"

TABLE = "e82e02f93055d6b0b544"  # tableEx: filter, sort by Rank, a projection captioned ' '
CARD_BARE = "fb4c5aa34e0b084b62b9"  # cardVisual whose sort's Property differs from its queryRef
CARD = "2202e311b1493069e4eb"
COLUMN = "b404a461ede7604a2774"  # columnChart: per-category fills, a sort, an interaction
COLUMN_2 = "da32cdbb0090e885649b"
CHANNEL_SLICER = "54be5cf0783e66471540"
STORE_SLICER = "5b0dbccd61c6c4adb415"
BUTTON = "eff2d7b04706cc92f1db"  # multipage only
GROUP = "40c896eda14bfa8f6a97"  # Header block: holds INNER_GROUP and a card
INNER_GROUP = "7799291338ca3548d7d5"  # Filters: two slicers
SLICER_A = "04ba32659627f6e8c7d7"  # in INNER_GROUP, stored (40, 60), page (120, 180)
SLICER_B = "2b5b8e3a7ed15138a983"
GROUP_CARD = "dfb4645ec076d1209ebe"  # in GROUP, stored (40, 140), page (80, 200)
IMAGE = "81ab76274a5b4e9e62da"


def load(name: str = "structure-twin") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def model(summarize: dict[tuple[str, str], str] | None = None) -> ModelInfo:
    tables = [
        ("DimEmployee", "EmployeeKey", "int64"),
        ("DimEmployee", "Salary", "float64"),
        ("DimEmployee", "Hired", "datetime64[ns]"),
        ("DimStore", "StoreName", "object"),
        ("DimStore", "StoreKey", "int64"),
        ("DimChannel", "ChannelName", "object"),
        ("Clustered Employees", "Clustered Employees", "object"),
    ]
    measures = ["Rank", "Table MTD Sales", "Employees Avg MTD Sales", "TopN/BottomN/ MTD Sales"]
    types: dict[str, Any] = {
        "columns": [
            {"table": table, "name": name, "summarizeBy": how}
            for (table, name), how in (summarize or {}).items()
        ]
    }
    return ModelInfo.from_report(
        [{"TableName": t, "ColumnName": c, "PandasDataType": d} for t, c, d in tables],
        [{"Name": m, "TableName": "# Measures"} for m in measures],
        types,
    )


def run(
    document: PbirDocument, *operations: dict[str, Any], info: ModelInfo | None = None
) -> report_edits.EditOutcome:
    parsed = report_edits.parse_operations(list(operations))
    return report_edits.apply_operations(document, list(parsed), info or model())


def added(document: PbirDocument, before: set[str]) -> tuple[PageDoc, dict[str, Any]]:
    (name,) = document.visual_names() - before
    return document.visual(name)


def field(name: str, table: str = "# Measures", **more: Any) -> dict[str, Any]:
    return {"table": table, "name": name, **more}


CHANNEL = field("ChannelName", "DimChannel")
STORE = field("StoreName", "DimStore")
RANK = field("Rank")
SALES = field("Table MTD Sales")


def state(visual: dict[str, Any]) -> dict[str, list[str]]:
    """role -> the fields' captions, for reading a query state at a glance."""
    found = visual["visual"]["query"]["queryState"]
    return {
        role: [p["nativeQueryRef"] for p in bucket["projections"]] for role, bucket in found.items()
    }


def stored(document: PbirDocument, name: str) -> dict[str, Any]:
    position: dict[str, Any] = document.visual(name)[1]["position"]
    return position


def box_of(document: PbirDocument, name: str) -> tuple[float, float, float, float]:
    page, _ = document.visual(name)
    box = page_box(page, name)
    return box.x, box.y, box.width, box.height


# --- wiring -------------------------------------------------------------------------------


def test_the_addable_types_are_the_role_tables_and_the_old_name_still_imports() -> None:
    assert report_edit_layout.ADDABLE_TYPES is report_roles.ADDABLE
    assert report_edits.ADDABLE_TYPES is report_roles.ADDABLE
    assert "cardVisual" in report_edits.ADDABLE_TYPES
    for legacy in ("_assign_roles", "_drop_selectors", "_apply_add_visual", "_apply_set_position"):
        assert hasattr(report_edits, legacy)


def test_the_layout_family_lists_its_nine_operations_and_the_six_new_ones() -> None:
    names = [str(cls.model_fields["op"].default) for cls in report_edit_layout.LAYOUT_OPERATIONS]
    assert names == [
        "set_position",
        "arrange",
        "set_visual_hidden",
        "delete_visual",
        "duplicate_visual",
        "add_visual",
        "change_visual_type",
        "set_fields",
        "set_sort",
        "align_visuals",
        "distribute_visuals",
        "swap_visuals",
        "place_visual",
        "add_textbox",
        "add_shape",
    ]
    assert sorted(report_edit_layout.LAYOUT_HANDLERS) == sorted(names)
    assert set(report_edit_align.ALIGN_HANDLERS) <= set(report_edits._HANDLERS)


def test_the_legacy_role_assignment_now_uses_the_role_table() -> None:
    projections = [
        ("measure", {"nativeQueryRef": "Sales"}),
        ("measure", {"nativeQueryRef": "Target"}),
    ]
    # A kpi's first measure is the value (Indicator), not the alphabetically first well (Goal).
    assert report_edit_layout._assign_roles("kpi", projections) == {
        "Indicator": [{"nativeQueryRef": "Sales"}],
        "Goal": [{"nativeQueryRef": "Target"}],
    }
    with pytest.raises(DocumentError, match="A card holds measures"):
        report_edit_layout._assign_roles("card", [("column", {"nativeQueryRef": "Store"})])


# --- AddVisual ------------------------------------------------------------------------------

LAYOUTS = [
    ("card", [RANK], {"Values": ["Rank"]}),
    ("cardVisual", [RANK, SALES], {"Data": ["Rank", "Table MTD Sales"]}),
    ("multiRowCard", [CHANNEL, RANK], {"Values": ["ChannelName", "Rank"]}),
    ("columnChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    (
        "clusteredColumnChart",
        [CHANNEL, RANK, SALES],
        {"Category": ["ChannelName"], "Y": ["Rank", "Table MTD Sales"]},
    ),
    (
        "hundredPercentStackedColumnChart",
        [CHANNEL, RANK],
        {"Category": ["ChannelName"], "Y": ["Rank"]},
    ),
    ("barChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("clusteredBarChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    (
        "hundredPercentStackedBarChart",
        [CHANNEL, RANK],
        {"Category": ["ChannelName"], "Y": ["Rank"]},
    ),
    ("lineChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("areaChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("stackedAreaChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("ribbonChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("pieChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("donutChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    ("tableEx", [CHANNEL, RANK, SALES], {"Values": ["ChannelName", "Rank", "Table MTD Sales"]}),
    (
        "pivotTable",
        [CHANNEL, RANK, SALES],
        {"Rows": ["ChannelName"], "Values": ["Rank", "Table MTD Sales"]},
    ),
    ("slicer", [CHANNEL], {"Values": ["ChannelName"]}),
    ("treemap", [CHANNEL, RANK], {"Group": ["ChannelName"], "Values": ["Rank"]}),
    ("funnel", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
    # A gauge's second measure is its target, a kpi's is its goal: value first, never swapped.
    ("gauge", [RANK, SALES], {"Y": ["Rank"], "TargetValue": ["Table MTD Sales"]}),
    (
        "kpi",
        [CHANNEL, RANK, SALES],
        {"TrendLine": ["ChannelName"], "Indicator": ["Rank"], "Goal": ["Table MTD Sales"]},
    ),
    # The combo's first measure is the columns, the next one a line on the second axis.
    (
        "lineClusteredColumnComboChart",
        [CHANNEL, RANK, SALES],
        {"Category": ["ChannelName"], "Y": ["Rank"], "Y2": ["Table MTD Sales"]},
    ),
    (
        "lineStackedColumnComboChart",
        [CHANNEL, RANK, SALES],
        {"Category": ["ChannelName"], "Y": ["Rank"], "Y2": ["Table MTD Sales"]},
    ),
    # Scatter: measures fill X then Y, and a column becomes the point category.
    (
        "scatterChart",
        [STORE, RANK, SALES],
        {"Category": ["StoreName"], "X": ["Rank"], "Y": ["Table MTD Sales"]},
    ),
    ("waterfallChart", [CHANNEL, RANK], {"Category": ["ChannelName"], "Y": ["Rank"]}),
]


def test_every_addable_type_has_a_layout_below() -> None:
    assert {t for t, _, _ in LAYOUTS} == set(report_roles.ADDABLE)


@pytest.mark.parametrize(("visual_type", "fields", "expected"), LAYOUTS)
def test_add_visual_puts_each_field_in_its_role(
    visual_type: str, fields: list[dict[str, Any]], expected: dict[str, list[str]]
) -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {"op": "add_visual", "page": DETAILS, "visual_type": visual_type, "fields": fields},
    )
    _, visual = added(document, before)
    assert visual["visual"]["visualType"] == visual_type
    assert state(visual) == expected


def test_add_visual_writes_exactly_what_desktop_starts_a_visual_from() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    schema = document.schema("visualContainer")
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "clusteredColumnChart",
            "fields": [CHANNEL, RANK],
        },
    )
    _, visual = added(document, before)
    # Free space beside the group (its members reach x=540, plus the 10 px gap), z above every
    # visual on the page (7000) and the next tab stop after the highest (7000).
    assert visual == {
        "$schema": schema,
        "name": visual["name"],
        "position": {"x": 550, "y": 0, "z": 8000, "height": 300, "width": 400, "tabOrder": 8000},
        "visual": {
            "visualType": "clusteredColumnChart",
            "query": {
                "queryState": {
                    "Category": {
                        "projections": [
                            {
                                "field": {
                                    "Column": {
                                        "Expression": {"SourceRef": {"Entity": "DimChannel"}},
                                        "Property": "ChannelName",
                                    }
                                },
                                "queryRef": "DimChannel.ChannelName",
                                "nativeQueryRef": "ChannelName",
                            }
                        ]
                    },
                    "Y": {
                        "projections": [
                            {
                                "field": {
                                    "Measure": {
                                        "Expression": {"SourceRef": {"Entity": "# Measures"}},
                                        "Property": "Rank",
                                    }
                                },
                                "queryRef": "# Measures.Rank",
                                "nativeQueryRef": "Rank",
                            }
                        ]
                    },
                }
            },
            "drillFilterOtherVisuals": True,
        },
    }
    assert page.visuals[visual["name"]] is visual
    assert outcome.summaries == [
        "Added a clustered column chart of ChannelName and Rank to page 'Details'. "
        "Power BI titles it 'Rank by ChannelName' by itself."
    ]


def test_a_new_visual_is_placed_where_it_overlaps_no_other_visual() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    taken = list(occupied(page).values())
    before = document.visual_names()
    outcome = run(
        document,
        {"op": "add_visual", "page": DETAILS, "visual_type": "tableEx", "fields": [CHANNEL, RANK]},
    )
    new_page, visual = added(document, before)
    box = page_box(new_page, visual["name"])
    assert overlaps(box, taken) == []
    # Not the old default corner (20, 20), where a new chart covered the slicers.
    assert (box.x, box.y) != (20, 20)
    assert (box.width, box.height) == (600, 350)
    assert outcome.warnings == []


def test_a_full_page_gets_the_least_covered_spot_and_an_advisory() -> None:
    document = load()
    before = document.visual_names()
    outcome = run(
        document,
        {"op": "add_visual", "page": PAGE1, "visual_type": "tableEx", "fields": [CHANNEL, RANK]},
    )
    page, visual = added(document, before)
    box = page_box(page, visual["name"])
    others = [b for n, b in leaf_boxes(page).items() if n != visual["name"]]
    assert overlaps(box, others)  # the page has no free 600 x 350 hole
    assert outcome.warnings[0] == (
        "There is no free space left on the page, so it was put where it covers the least."
    )
    assert outcome.warnings[1].endswith(".") and " overlaps " in outcome.warnings[1]


def test_an_explicit_position_is_honored_clamped_to_the_page_and_still_advised() -> None:
    document = load()
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "card",
            "fields": [RANK],
            "x": 5000,
            "y": 5000,
            "width": 300,
            "height": 100,
        },
    )
    page, visual = added(document, before)
    assert (visual["position"]["x"], visual["position"]["y"]) == (1620, 1100)
    assert (visual["position"]["width"], visual["position"]["height"]) == (300, 100)
    assert any("Table MTD Sales" not in w and "overlaps" in w for w in outcome.warnings)


def test_tab_order_is_the_highest_tab_stop_plus_a_thousand_not_a_function_of_z() -> None:
    document = load("multipage")
    document.visual(BUTTON)[1]["position"]["tabOrder"] = 40000
    before = document.visual_names()
    run(document, {"op": "add_visual", "page": PAGE1, "visual_type": "card", "fields": [RANK]})
    _, visual = added(document, before)
    assert visual["position"]["tabOrder"] == 41000
    assert visual["position"]["z"] == 16000  # the button is at z 15000


def test_a_new_visual_gets_no_invented_title() -> None:
    document = load("multipage")
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "columnChart",
            "fields": [CHANNEL, RANK],
        },
    )
    _, visual = added(document, before)
    assert "visualContainerObjects" not in visual["visual"]
    assert outcome.summaries[0].endswith("Power BI titles it 'Rank by ChannelName' by itself.")


def test_a_given_title_is_written_and_shown() -> None:
    document = load("multipage")
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "columnChart",
            "fields": [CHANNEL, RANK],
            "title": "Rank by channel",
        },
    )
    _, visual = added(document, before)
    assert visual["visual"]["visualContainerObjects"] == {
        "title": [
            {
                "properties": {
                    "text": {"expr": {"Literal": {"Value": "'Rank by channel'"}}},
                    "show": {"expr": {"Literal": {"Value": "true"}}},
                }
            }
        ]
    }
    assert outcome.summaries[0].endswith("Its title is 'Rank by channel'.")


def test_tables_and_cards_are_not_said_to_have_an_automatic_title() -> None:
    document = load("multipage")
    outcome = run(
        document,
        {"op": "add_visual", "page": DETAILS, "visual_type": "tableEx", "fields": [CHANNEL]},
        {"op": "add_visual", "page": DETAILS, "visual_type": "card", "fields": [RANK]},
    )
    assert all("titles it" not in s for s in outcome.summaries)


def test_a_number_with_no_default_summary_asks_how_to_summarize_it() -> None:
    document = load("multipage")
    fields = [STORE, field("Salary", "DimEmployee")]
    with pytest.raises(NeedsChoice) as asked:
        run(
            document,
            {"op": "add_visual", "page": DETAILS, "visual_type": "pieChart", "fields": fields},
        )
    error = asked.value
    assert error.step == 1
    assert (
        error.question == "How should Salary be summarized? Power BI has no default summary for it."
    )
    assert [c.label for c in error.options] == [
        "Sum of Salary",
        "Average of Salary",
        "Count (distinct) of Salary",
        "Minimum of Salary",
        "Maximum of Salary",
    ]
    assert error.options[1].fill == {
        "fields": [STORE, {"table": "DimEmployee", "name": "Salary", "aggregation": "Average"}]
    }
    assert document.visual_names() == load("multipage").visual_names()  # nothing was written
    # The answer, applied, builds the visual with the chosen function.
    before = document.visual_names()
    run(
        document,
        {"op": "add_visual", "page": DETAILS, "visual_type": "pieChart", **error.options[1].fill},
    )
    _, visual = added(document, before)
    (projection,) = visual["visual"]["query"]["queryState"]["Y"]["projections"]
    assert projection["field"]["Aggregation"]["Function"] == 1
    assert (projection["queryRef"], projection["nativeQueryRef"]) == (
        "Average(DimEmployee.Salary)",
        "Average of Salary",
    )


def test_a_number_uses_its_default_summary_when_the_model_has_one() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "pieChart",
            "fields": [STORE, field("Salary", "DimEmployee")],
        },
        info=model({("DimEmployee", "Salary"): "sum"}),
    )
    _, visual = added(document, before)
    (projection,) = visual["visual"]["query"]["queryState"]["Y"]["projections"]
    assert projection["field"]["Aggregation"]["Function"] == 0
    assert projection["nativeQueryRef"] == "Sum of Salary"


def test_a_number_stays_a_plain_column_where_the_visual_can_hold_it() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "tableEx",
            "fields": [STORE, field("Salary", "DimEmployee")],
        },
    )
    _, visual = added(document, before)
    assert "Column" in visual["visual"]["query"]["queryState"]["Values"]["projections"][1]["field"]


def test_an_explicit_aggregation_is_written_as_desktop_writes_it() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "columnChart",
            "fields": [STORE, field("Salary", "DimEmployee", aggregation="Max")],
        },
    )
    _, visual = added(document, before)
    (projection,) = visual["visual"]["query"]["queryState"]["Y"]["projections"]
    assert projection == {
        "field": {
            "Aggregation": {
                "Expression": {
                    "Column": {
                        "Expression": {"SourceRef": {"Entity": "DimEmployee"}},
                        "Property": "Salary",
                    }
                },
                "Function": 4,
            }
        },
        "queryRef": "Max(DimEmployee.Salary)",
        "nativeQueryRef": "Max of Salary",
    }


@pytest.mark.parametrize(
    ("visual_type", "fields", "message"),
    [
        ("card", [STORE], "A card holds measures, so StoreName cannot go in it."),
        ("card", [RANK, SALES], "A card has no room for Table MTD Sales."),
        ("slicer", [RANK], "A slicer holds columns, so Rank cannot go in it."),
        ("clusteredColumnChart", [STORE], "A clustered column chart needs a field for Y-axis."),
        (
            "waterfallChart",
            [CHANNEL, RANK, SALES],
            "A waterfall chart has no room for Table MTD Sales.",
        ),
        (
            "cardVisual",
            [STORE, RANK],
            "A card holds measures, so StoreName cannot go in it as a category. "
            "Use small multiples to get one card per value.",
        ),
    ],
)
def test_a_placement_that_cannot_work_is_a_plain_sentence(
    visual_type: str, fields: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load("multipage"),
            {"op": "add_visual", "page": DETAILS, "visual_type": visual_type, "fields": fields},
        )
    assert str(raised.value) == message


def test_small_multiples_take_a_column_only_when_named() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {
            "op": "add_visual",
            "page": DETAILS,
            "visual_type": "cardVisual",
            "fields": [RANK],
            "small_multiples": [STORE],
        },
    )
    _, visual = added(document, before)
    assert state(visual) == {"Data": ["Rank"], "Rows": ["StoreName"]}
    with pytest.raises(
        DocumentError, match="needs a column for small multiples, and Rank is a measure"
    ):
        run(
            document,
            {
                "op": "add_visual",
                "page": DETAILS,
                "visual_type": "cardVisual",
                "fields": [SALES],
                "small_multiples": [RANK],
            },
        )


def test_a_text_box_shape_or_picture_is_not_added_from_fields() -> None:
    for visual_type in ("textbox", "shape", "image"):
        with pytest.raises(DocumentError, match="not fields"):
            run(
                load(),
                {"op": "add_visual", "page": PAGE1, "visual_type": visual_type, "fields": [RANK]},
            )
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "add_visual", "page": PAGE1, "visual_type": "map", "fields": [STORE]})
    assert str(raised.value) == "Studio cannot add a map visual yet"


def test_an_unknown_page_lists_the_pages() -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load("multipage"),
            {"op": "add_visual", "page": "Nowhere", "visual_type": "card", "fields": [RANK]},
        )
    assert str(raised.value) == (
        "There is no page named 'Nowhere'. Pages: '1', 'Details', 'Store drillthrough' and "
        "'Sales tooltip'."
    )


def test_a_visual_studio_does_not_draw_says_it_shows_in_power_bi() -> None:
    document = load("multipage")
    outcome = run(
        document,
        {"op": "add_visual", "page": DETAILS, "visual_type": "treemap", "fields": [CHANNEL, RANK]},
        {"op": "add_visual", "page": DETAILS, "visual_type": "card", "fields": [RANK]},
    )
    assert outcome.visible_in == [
        VisibleIn(
            "add_visual",
            "powerbi_only",
            "Studio does not draw a treemap yet; it shows in Power BI.",
        )
    ]


# --- ChangeVisualType --------------------------------------------------------------------


def test_changing_a_type_carries_the_formatting_and_lists_what_was_dropped() -> None:
    document = load()
    page, visual = document.visual(COLUMN)
    box, container = dict(visual["position"]), visual["visual"].get("visualContainerObjects")
    old_objects = set(visual["visual"]["objects"])
    outcome = run(
        document, {"op": "change_visual_type", "visual": COLUMN, "visual_type": "lineChart"}
    )
    assert outcome.summaries == [
        "Changed the 'Total MTD Sales' stacked column chart into a line chart. "
        "Kept 9 formatting settings. "
        "Dropped 1 setting the new type does not have: Category axis (some options)."
    ]
    body = visual["visual"]
    assert body["visualType"] == "lineChart"
    assert set(body["objects"]) == old_objects  # nothing was popped; the axis keeps what a line has
    assert state(visual) == {"Category": ["Clustered Employees"], "Y": ["TopN/BottomN/ MTD Sales"]}
    assert body.get("visualContainerObjects") == container
    assert visual["position"] == box
    assert body["query"]["sortDefinition"]["sort"][0]["direction"] == "Descending"


def test_changing_a_type_keeps_the_group_the_filters_and_the_position() -> None:
    document = load("multipage")
    _, visual = document.visual(SLICER_A)
    visual["filterConfig"] = {"filters": [{"name": "Filter00000000000000000001"}]}
    position = dict(visual["position"])
    run(document, {"op": "change_visual_type", "visual": SLICER_A, "visual_type": "tableEx"})
    assert visual["parentGroupName"] == INNER_GROUP
    assert visual["filterConfig"] == {"filters": [{"name": "Filter00000000000000000001"}]}
    assert visual["position"] == position
    assert state(visual) == {"Values": ["Store"]}


def test_a_field_the_new_type_cannot_hold_is_left_out_with_its_sort_and_formatting() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    outcome = run(document, {"op": "change_visual_type", "visual": TABLE, "visual_type": "card"})
    assert "sortDefinition" not in visual["visual"]["query"]  # the sort was on Rank, which left
    assert sum(len(v["projections"]) for v in visual["visual"]["query"]["queryState"].values()) == 1
    assert "Left out Agent ID: a card holds measures." in outcome.summaries[0]
    assert "Removed the sort by Rank." in outcome.summaries[0]


def test_a_category_is_never_moved_to_small_multiples_by_a_type_change() -> None:
    document = load()
    _, visual = document.visual(COLUMN)
    outcome = run(
        document, {"op": "change_visual_type", "visual": COLUMN, "visual_type": "cardVisual"}
    )
    assert state(visual) == {"Data": ["TopN/BottomN/ MTD Sales"]}
    assert "Rows" not in visual["visual"]["query"]["queryState"]
    assert outcome.warnings == [
        "Clustered Employees was left out. On a card a category becomes small multiples, one "
        "card per value; ask for small multiples if that is what you want."
    ]


@pytest.mark.parametrize(
    ("visual", "visual_type", "message"),
    [
        (
            GROUP,
            "lineChart",
            "The group 'Header block' holds other visuals; edit the visuals inside it",
        ),
        (COLUMN, "map", "Studio cannot turn a visual into a map yet"),
    ],
)
def test_a_type_change_that_cannot_be_done_is_refused(
    visual: str, visual_type: str, message: str
) -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load("multipage"),
            {"op": "change_visual_type", "visual": visual, "visual_type": visual_type},
        )
    assert str(raised.value) == message


def test_changing_to_the_same_type_says_so() -> None:
    document = load()
    outcome = run(
        document, {"op": "change_visual_type", "visual": COLUMN, "visual_type": "columnChart"}
    )
    assert outcome.summaries == [
        "The 'Total MTD Sales' stacked column chart already is a stacked column chart."
    ]


# --- SetFields and SetSort ------------------------------------------------------------------


def test_removing_a_measure_removes_a_sort_whose_property_was_renamed() -> None:
    document = load()
    _, visual = document.visual(CARD_BARE)
    projection = visual["visual"]["query"]["queryState"]["Data"]["projections"][0]
    # The fb4c5aa3 shape: the projection's queryRef kept the old measure name, the field and
    # the sort item carry the new one, so no Entity.Property string matches the queryRef.
    assert projection["queryRef"] == "# Measures.Number of Employees with Positive Change %"
    assert visual["visual"]["query"]["sortDefinition"]["sort"][0]["field"]["Measure"][
        "Property"
    ] == ("Employees with Positive Change %")
    outcome = run(
        document,
        {
            "op": "set_fields",
            "visual": CARD_BARE,
            "role": "Data",
            "mode": "remove",
            "fields": [field("Employees with Positive Change %")],
        },
        info=_with_measure("Employees with Positive Change %"),
    )
    assert "sortDefinition" not in visual["visual"]["query"]
    assert (
        "queryState" in visual["visual"]["query"] and visual["visual"]["query"]["queryState"] == {}
    )
    labels = [
        e
        for e in visual["visual"].get("objects", {}).get("label", [])
        if e.get("selector", {}).get("metadata") == projection["queryRef"]
    ]
    assert labels == []
    assert outcome.summaries == [
        "Removed Employees with Positive Change % from Value of the 'Agents with Positive "
        "Change %' card. Removed the sort by Employees with Positive Change %. "
        "Removed 23 formatting settings for Employees with Positive Change %."
    ]


def _with_measure(name: str) -> ModelInfo:
    info = model()
    info.measures[name] = "# Measures"
    return info


def test_a_field_captioned_with_a_blank_is_found_by_what_it_is() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    projections = visual["visual"]["query"]["queryState"]["Values"]["projections"]
    assert projections[-1]["nativeQueryRef"] == " "
    run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "Values",
            "mode": "remove",
            "fields": [field("vs Previous Month")],
        },
        info=_with_measure("vs Previous Month"),
    )
    left = [p["queryRef"] for p in visual["visual"]["query"]["queryState"]["Values"]["projections"]]
    assert "# Measures.vs PM" not in left and len(left) == 5


def test_removing_by_display_name_and_by_table_and_column_both_work() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "Values",
            "mode": "remove",
            "fields": [field("EmployeeKey", "DimEmployee")],
        },
    )
    names = [
        p["nativeQueryRef"]
        for p in visual["visual"]["query"]["queryState"]["Values"]["projections"]
    ]
    assert names == ["Agent Name", "Rank", "MTD Sales", "vs PM %", " "]


def test_replace_keeps_an_existing_field_as_it_is_and_drops_the_rest() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    keep = visual["visual"]["query"]["queryState"]["Values"]["projections"][0]
    outcome = run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "values",
            "mode": "replace",
            "fields": [field("EmployeeKey", "DimEmployee"), field("Table MTD Sales")],
        },
    )
    projections = visual["visual"]["query"]["queryState"]["Values"]["projections"]
    assert projections[0] is keep and projections[0]["displayName"] == "Agent ID"
    assert [p["queryRef"] for p in projections] == [
        "DimEmployee.EmployeeKey",
        "# Measures.Table MTD Sales",
    ]
    assert "sortDefinition" not in visual["visual"]["query"]  # it sorted by Rank, which went
    assert outcome.summaries == [
        "Set Columns of the table of Agent ID to Agent ID and MTD Sales. Removed the sort by "
        "Rank. Removed 4 formatting settings for vs PM %. Removed 4 formatting settings for "
        "Rank. Removed 2 formatting settings for vs Previous Month. Removed 1 formatting "
        "setting for Agent Name."
    ]


def test_a_number_added_to_a_value_role_uses_its_default_summary_or_asks() -> None:
    salary = field("Salary", "DimEmployee")
    document = load()
    _, visual = document.visual(COLUMN)
    run(
        document,
        {"op": "set_fields", "visual": COLUMN, "role": "Y", "mode": "add", "fields": [salary]},
        info=model({("DimEmployee", "Salary"): "average"}),
    )
    added_projection = visual["visual"]["query"]["queryState"]["Y"]["projections"][1]
    assert added_projection["queryRef"] == "Average(DimEmployee.Salary)"
    with pytest.raises(NeedsChoice) as asked:
        run(
            load(),
            {"op": "set_fields", "visual": COLUMN, "role": "Y", "mode": "add", "fields": [salary]},
            info=model({("DimEmployee", "Salary"): "none"}),
        )
    assert asked.value.options[0].fill["fields"] == [{**salary, "aggregation": "Sum"}]
    assert asked.value.question.startswith("How should Salary be summarized?")


def test_a_text_column_on_a_value_role_is_counted_not_added_bare() -> None:
    with pytest.raises(NeedsChoice) as asked:
        run(
            load(),
            {"op": "set_fields", "visual": COLUMN, "role": "Y", "mode": "add", "fields": [STORE]},
        )
    assert [c.label for c in asked.value.options] == [
        "Count (distinct) of StoreName",
        "Count of StoreName",
    ]


def test_a_measure_cannot_go_in_a_role_that_needs_a_column() -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load(),
            {
                "op": "set_fields",
                "visual": COLUMN,
                "role": "Category",
                "mode": "add",
                "fields": [RANK],
            },
        )
    assert str(raised.value) == "Rank is a measure, but X-axis needs a column."


def test_an_unknown_field_well_lists_the_ones_there_are() -> None:
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "set_fields", "visual": COLUMN, "role": "Sparkle", "fields": [RANK]})
    assert str(raised.value) == (
        "A stacked column chart has no 'Sparkle' field well. "
        "Its field wells are X-axis, Legend, Y-axis, Small multiples and Tooltips."
    )


def test_removing_a_field_that_is_not_there_says_what_the_well_holds() -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load(),
            {"op": "set_fields", "visual": COLUMN, "role": "Y", "mode": "remove", "fields": [RANK]},
        )
    assert (
        str(raised.value) == "None of those fields are in Y-axis. It holds TopN/BottomN/ MTD Sales."
    )


DATE_HIERARCHY = {
    "field": {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {
                        "PropertyVariationSource": {
                            "Expression": {"SourceRef": {"Entity": "DimDate"}},
                            "Name": "Variation",
                            "Property": "Date",
                        }
                    },
                    "Hierarchy": "Date Hierarchy",
                }
            },
            "Level": "Year",
        }
    },
    "queryRef": "DimDate.Date.Variation.Date Hierarchy.Year",
    "nativeQueryRef": "Date Hierarchy Year",
}
USER_HIERARCHY = {
    "field": {
        "HierarchyLevel": {
            "Expression": {
                "Hierarchy": {
                    "Expression": {"SourceRef": {"Entity": "DimDate"}},
                    "Hierarchy": "Calendar",
                }
            },
            "Level": "Year",
        }
    },
    "queryRef": "DimDate.Calendar.Year",
    "nativeQueryRef": "Year",
}


def test_a_hierarchy_level_is_removed_by_its_table_and_level() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    bucket = visual["visual"]["query"]["queryState"]["Values"]["projections"]
    bucket.append(USER_HIERARCHY)
    outcome = run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "Values",
            "mode": "remove",
            "fields": [{"table": "DimDate", "name": "Year"}],
        },
    )
    assert USER_HIERARCHY not in visual["visual"]["query"]["queryState"]["Values"]["projections"]
    assert outcome.summaries[0].startswith("Removed Year from Columns of")


def test_a_field_studio_cannot_edit_is_refused_with_an_explanation() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    bucket = visual["visual"]["query"]["queryState"]["Values"]["projections"]
    bucket.append(DATE_HIERARCHY)
    with pytest.raises(DocumentError) as raised:
        run(
            document,
            {
                "op": "set_fields",
                "visual": TABLE,
                "role": "Values",
                "mode": "remove",
                "fields": [{"table": "DimDate", "name": "Date Hierarchy Year"}],
            },
        )
    assert str(raised.value) == (
        "Date Hierarchy Year is an automatic date hierarchy, which Studio leaves as it is. "
        "Change it in Power BI Desktop."
    )
    # Replacing the whole well would drop it too.
    with pytest.raises(DocumentError, match="automatic date hierarchy"):
        run(
            document,
            {
                "op": "set_fields",
                "visual": TABLE,
                "role": "Values",
                "mode": "replace",
                "fields": [RANK],
            },
        )
    bucket.pop()
    bucket.append(
        {
            "field": {"NativeVisualCalculation": {"Name": "Running total", "Expression": "1"}},
            "queryRef": "Running total",
            "nativeQueryRef": "Running total",
        }
    )
    with pytest.raises(DocumentError, match="Running total is a visual calculation"):
        run(
            document,
            {
                "op": "set_fields",
                "visual": TABLE,
                "role": "Values",
                "mode": "remove",
                "fields": [{"table": "T", "name": "Running total"}],
            },
        )


def test_a_field_parameter_column_is_never_edited_or_added() -> None:
    info = model()
    info.calculated["Params"] = "{(\"A\", NAMEOF('# Measures'[Rank]), 0)}"
    info.columns["Params"] = {"Params": "object"}
    document = load()
    _, visual = document.visual(TABLE)
    parameter = {
        "field": {
            "Column": {"Expression": {"SourceRef": {"Entity": "Params"}}, "Property": "Params"}
        },
        "queryRef": "Params.Params",
        "nativeQueryRef": "Params",
    }
    visual["visual"]["query"]["queryState"]["Values"]["projections"].append(parameter)
    with pytest.raises(DocumentError, match="Params is a field parameter"):
        run(
            document,
            {
                "op": "set_fields",
                "visual": TABLE,
                "role": "Values",
                "mode": "remove",
                "fields": [{"table": "Params", "name": "Params"}],
            },
            info=info,
        )
    with pytest.raises(DocumentError, match="Params belongs to a field parameter"):
        run(
            load(),
            {
                "op": "set_fields",
                "visual": TABLE,
                "role": "Values",
                "mode": "add",
                "fields": [{"table": "Params", "name": "Params"}],
            },
            info=info,
        )


def test_set_fields_on_a_group_is_refused_by_name() -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load("multipage"),
            {"op": "set_fields", "visual": GROUP, "role": "Values", "fields": [RANK]},
        )
    assert (
        str(raised.value)
        == "The group 'Header block' holds other visuals; edit the visuals inside it"
    )


def test_sort_uses_the_fields_own_expression_and_replaces_the_sort() -> None:
    document = load()
    _, visual = document.visual(COLUMN)
    run(
        document,
        {
            "op": "set_sort",
            "visual": COLUMN,
            "field": field("TopN/BottomN/ MTD Sales"),
            "direction": "ascending",
        },
    )
    assert visual["visual"]["query"]["sortDefinition"] == {
        "sort": [
            {
                "field": {
                    "Measure": {
                        "Expression": {"SourceRef": {"Entity": "# Measures"}},
                        "Property": "TopN/BottomN/ MTD Sales",
                    }
                },
                "direction": "Ascending",
            }
        ],
    }


def test_sort_by_an_aggregated_column_keeps_it_an_aggregation() -> None:
    document = load()
    _, visual = document.visual(COLUMN)
    run(
        document,
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "mode": "replace",
            "fields": [field("Salary", "DimEmployee", aggregation="Max")],
        },
        {
            "op": "set_sort",
            "visual": COLUMN,
            "field": field("Salary", "DimEmployee", aggregation="Max"),
            "direction": "descending",
        },
    )
    (item,) = visual["visual"]["query"]["sortDefinition"]["sort"]
    assert item["field"]["Aggregation"]["Function"] == 4
    assert item["direction"] == "Descending"


def test_sort_by_a_hierarchy_level_and_the_two_ways_it_fails() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    visual["visual"]["query"]["queryState"]["Values"]["projections"].append(USER_HIERARCHY)
    run(
        document, {"op": "set_sort", "visual": TABLE, "field": {"table": "DimDate", "name": "Year"}}
    )
    assert (
        visual["visual"]["query"]["sortDefinition"]["sort"][0]["field"] == USER_HIERARCHY["field"]
    )
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "set_sort", "visual": COLUMN, "field": RANK})
    assert str(raised.value) == "Add Rank to the visual before sorting by it"
    with pytest.raises(DocumentError, match="has no column or measure named 'Ghost'"):
        run(load(), {"op": "set_sort", "visual": COLUMN, "field": field("Ghost", "DimStore")})


# --- DeleteVisual -----------------------------------------------------------------------


def dangling(document: PbirDocument) -> list[str]:
    """Visuals whose group is not on their page (a leftover parentGroupName)."""
    return [
        name
        for page in document.pages.values()
        for name, visual in page.visuals.items()
        if visual.get("parentGroupName") not in (None, *page.visuals)
    ]


def test_deleting_a_visual_removes_its_interactions_and_bookmark_entries() -> None:
    document = load("multipage")
    page = document.pages[PAGE1]
    assert len(page.data["visualInteractions"]) == 2
    outcome = run(document, {"op": "delete_visual", "visual": COLUMN})
    assert COLUMN not in page.visuals
    assert page.data["visualInteractions"] == [
        {"source": STORE_SLICER, "target": TABLE, "type": "DataFilter"}
    ]
    bookmark = document.bookmark("Channel focus")
    assert bookmark["options"]["targetVisualNames"] == [CHANNEL_SLICER]
    assert COLUMN not in bookmark["explorationState"]["sections"][PAGE1]["visualContainers"]
    assert not ReferenceIndex.build(document).visual_uses(COLUMN)
    assert outcome.summaries == [
        "Deleted the 'Total MTD Sales' stacked column chart. "
        "It was also removed from 1 interaction setting and 1 bookmark."
    ]
    assert f"Report/definition/pages/{PAGE1}/visuals/{COLUMN}/visual.json" not in _committed(
        document
    )


def _committed(document: PbirDocument) -> set[str]:
    document.commit()
    return set(document.package.names())


def test_deleting_a_group_deletes_everything_in_it_including_nested_groups() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    outcome = run(document, {"op": "delete_visual", "visual": GROUP})
    assert set(page.visuals) == {IMAGE, "d6c05c8319906941fe02"}
    assert dangling(document) == []
    for gone in (GROUP, INNER_GROUP, SLICER_A, SLICER_B, GROUP_CARD):
        assert not ReferenceIndex.build(document).visual_uses(gone)
    assert outcome.summaries == [
        "Deleted the group 'Header block' and the 4 visuals in it. "
        "It was also removed from 1 bookmark."
    ]
    names = _committed(document)
    assert not any(GROUP_CARD in n or SLICER_A in n for n in names)


def test_deleting_an_inner_group_leaves_the_outer_group_and_no_dangling_reference() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    run(document, {"op": "delete_visual", "visual": INNER_GROUP})
    assert set(page.visuals) >= {GROUP, GROUP_CARD}
    assert SLICER_A not in page.visuals and INNER_GROUP not in page.visuals
    assert dangling(document) == []
    bookmark = document.bookmark("Details view")
    assert bookmark["options"]["targetVisualNames"] == [GROUP_CARD]


def test_a_group_left_empty_goes_with_its_last_member() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    outcome = run(
        document,
        {"op": "delete_visual", "visual": SLICER_A},
        {"op": "delete_visual", "visual": SLICER_B},
    )
    assert INNER_GROUP not in page.visuals and GROUP in page.visuals
    assert outcome.summaries[1] == (
        "Deleted the 'Country' slicer and the group 'Filters', which had nothing else in it."
    )
    assert dangling(document) == []


def test_a_missing_visual_lists_the_ones_there_are() -> None:
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "delete_visual", "visual": "nope"})
    assert str(raised.value) == (
        "There is no visual 'nope' in this report. Visuals in this report: the table of Agent ID, "
        "the slicer of Top-Bottom-N, the 'Top/Bottom Field' button slicer, the 'Agents with "
        "Positive Change %' card, the 'Agents Avg MTD Sales' card, the 'Agents Avg MTD % Change' "
        "card and 9 more."
    )


def test_a_visual_can_be_named_by_its_caption() -> None:
    document = load()
    run(document, {"op": "delete_visual", "visual": "Total MTD Sales"})
    assert COLUMN not in document.pages[PAGE1].visuals


# --- DuplicateVisual ------------------------------------------------------------------------


def all_filter_names(document: PbirDocument) -> list[str]:
    found: list[str] = []
    holders = [document.report, *(p.data for p in document.pages.values())]
    holders += [v for p in document.pages.values() for v in p.visuals.values()]
    holders += list(document.bookmarks().values())
    for holder in holders:
        config = holder.get("filterConfig")
        found += [f["name"] for f in (config or {}).get("filters", []) if "name" in f]
    return found


def test_a_duplicate_has_new_filter_names_a_new_tab_stop_and_a_free_place() -> None:
    document = load()
    page, table = document.visual(TABLE)
    old_filters = all_filter_names(document)
    assert old_filters
    before = document.visual_names()
    others = [b for n, b in leaf_boxes(page).items()]
    outcome = run(document, {"op": "duplicate_visual", "visual": TABLE})
    _, copy = added(document, before)
    names = all_filter_names(document)
    assert len(names) == len(set(names)) == len(old_filters) * 2
    assert all(re.fullmatch(r"Filter[0-9a-f]{20}", n) for n in names)
    assert copy["position"]["tabOrder"] == 15000  # highest tab stop (14000) plus a thousand
    assert copy["position"]["z"] == 15000
    assert copy["visual"] == table["visual"] and copy["name"] != TABLE
    assert "parentGroupName" not in copy
    assert outcome.summaries == ["Duplicated the table of Agent ID onto page '1'."]
    assert others  # (the page is full: the advisory says where it landed)
    assert outcome.warnings[0].startswith("There is no free space left on the page")


def test_a_duplicate_on_a_page_with_room_goes_beside_the_original() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    before = document.visual_names()
    outcome = run(document, {"op": "duplicate_visual", "visual": IMAGE})
    _, copy = added(document, before)
    source = page_box(page, IMAGE)
    box = page_box(page, copy["name"])
    assert (box.width, box.height) == (source.width, source.height)
    assert box.y >= source.y and box.x >= source.right
    assert overlaps(box, [b for n, b in occupied(page).items() if n != copy["name"]]) == []
    assert outcome.warnings == []


def test_an_explicit_spot_is_used_and_clamped_to_the_page() -> None:
    document = load()
    before = document.visual_names()
    run(document, {"op": "duplicate_visual", "visual": CARD, "x": 10, "y": 20})
    _, copy = added(document, before)
    assert (copy["position"]["x"], copy["position"]["y"]) == (10, 20)
    document = load("multipage")
    before = document.visual_names()
    run(document, {"op": "duplicate_visual", "visual": TABLE, "page": TOOLTIP})
    page, copy = added(document, before)
    assert page.name == TOOLTIP  # 894 x 623 on a 320 x 240 page: cut down to fit
    assert (copy["position"]["width"], copy["position"]["height"]) == (320, 240)
    assert (copy["position"]["x"], copy["position"]["y"]) == (0, 0)


def test_a_duplicated_group_member_leaves_its_group_and_uses_page_pixels() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(document, {"op": "duplicate_visual", "visual": SLICER_A})
    page, copy = added(document, before)
    assert "parentGroupName" not in copy
    box = page_box(page, copy["name"])
    assert (copy["position"]["x"], copy["position"]["y"]) == (box.x, box.y)  # top level: no origin
    assert (box.width, box.height) == (200, 60)


def test_a_duplicated_group_is_copied_with_everything_in_it_under_new_names() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    sources = {n: dict(v["position"]) for n, v in page.visuals.items()}
    before = document.visual_names()
    old_tabs = [v["position"]["tabOrder"] for v in page.visuals.values()]
    run(document, {"op": "duplicate_visual", "visual": GROUP})
    fresh = document.visual_names() - before
    assert len(fresh) == 5
    tops = [n for n in fresh if "parentGroupName" not in page.visuals[n]]
    assert len(tops) == 1
    for name in fresh - set(tops):
        assert page.visuals[name]["parentGroupName"] in fresh  # never an old group
    assert dangling(document) == []
    # Members keep their stored (relative) positions; only the top copy is placed.
    relative = sorted(
        (page.visuals[n]["position"]["x"], page.visuals[n]["position"]["y"])
        for n in fresh - set(tops)
    )
    inside = (INNER_GROUP, SLICER_A, SLICER_B, GROUP_CARD)
    assert relative == sorted((sources[n]["x"], sources[n]["y"]) for n in inside)
    tab_orders = sorted(page.visuals[n]["position"]["tabOrder"] for n in fresh)
    assert tab_orders == [max(old_tabs) + 1000 * i for i in range(1, 6)]
    frame = page_box(page, tops[0])
    assert overlaps(frame, [b for n, b in leaf_boxes(page).items() if n not in fresh]) == []


def test_a_duplicate_on_another_page_leaves_both_pages_interactions_alone() -> None:
    document = load("multipage")
    interactions = [dict(i) for i in document.pages[PAGE1].data["visualInteractions"]]
    before = document.visual_names()
    run(document, {"op": "duplicate_visual", "visual": COLUMN, "page": "Details"})
    page, copy = added(document, before)
    assert page.name == DETAILS
    assert document.pages[PAGE1].data["visualInteractions"] == interactions
    assert "visualInteractions" not in page.data
    assert not ReferenceIndex.build(document).visual_uses(copy["name"])
    assert len(document.visual_names()) == len(before) + 1


def test_duplicating_onto_an_unknown_page_lists_the_pages() -> None:
    with pytest.raises(
        DocumentError, match="There is no page named 'Nowhere'. Pages: '1', 'Details'"
    ):
        run(load("multipage"), {"op": "duplicate_visual", "visual": CARD, "page": "Nowhere"})


# --- SetPosition ----------------------------------------------------------------------------


def test_moving_a_group_member_converts_page_pixels_to_its_relative_position_and_back() -> None:
    document = load("multipage")
    assert box_of(document, SLICER_A) == (120, 180, 200, 60)  # stored (40, 60) + group origins
    outcome = run(document, {"op": "set_position", "visual": SLICER_A, "x": 500, "y": 300})
    assert (stored(document, SLICER_A)["x"], stored(document, SLICER_A)["y"]) == (420, 180)
    assert box_of(document, SLICER_A) == (500, 300, 200, 60)
    assert outcome.summaries == ["Moved the slicer of Store to (500, 300)."]


def test_moving_a_group_changes_only_the_group_and_its_members_follow() -> None:
    document = load("multipage")
    member_before = stored(document, SLICER_A).copy()
    outcome = run(document, {"op": "set_position", "visual": GROUP, "x": 100, "y": 100})
    assert (stored(document, GROUP)["x"], stored(document, GROUP)["y"]) == (100, 100)
    assert stored(document, SLICER_A) == member_before
    assert box_of(document, SLICER_A) == (180, 220, 200, 60)  # moved by (+60, +40) with the group
    assert outcome.summaries == ["Moved the group 'Header block' to (100, 100)."]


def test_resizing_a_group_is_refused() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "set_position", "visual": GROUP, "width": 500})
    assert str(raised.value) == (
        "The group 'Header block' cannot be resized here. "
        "Resize the members individually, or ungroup in Power BI Desktop."
    )
    assert stored(document, GROUP)["width"] == 420
    # Setting the size it already has is not a resize.
    run(document, {"op": "set_position", "visual": GROUP, "width": 420, "height": 200, "x": 41})
    assert stored(document, GROUP)["x"] == 41


def test_a_member_is_kept_inside_the_page_in_page_pixels() -> None:
    document = load("multipage")
    outcome = run(document, {"op": "set_position", "visual": SLICER_A, "x": 5000, "y": 5000})
    assert box_of(document, SLICER_A) == (1080, 660, 200, 60)  # 1280 x 720 page
    assert (stored(document, SLICER_A)["x"], stored(document, SLICER_A)["y"]) == (1000, 540)
    assert outcome.summaries[0].endswith("It was kept inside the page.")


def test_resizing_a_visual_writes_only_its_size_and_says_so() -> None:
    document = load()
    x_before = stored(document, CARD)["x"]
    outcome = run(document, {"op": "set_position", "visual": CARD, "width": 500, "height": 120})
    assert stored(document, CARD)["x"] == x_before
    assert (stored(document, CARD)["width"], stored(document, CARD)["height"]) == (500, 120)
    assert outcome.summaries == ["Resized the 'Agents Avg MTD Sales' card to 500 x 120."]


def test_a_move_that_lands_on_another_visual_is_an_advisory_not_an_error() -> None:
    document = load()
    outcome = run(document, {"op": "set_position", "visual": CARD, "x": 173, "y": 154})
    # The card moved onto the first card's spot: 'now overlaps' names it.
    assert outcome.warnings == [
        "The 'Agents Avg MTD Sales' card now overlaps the 'Agents with Positive Change %' card."
    ]


def test_position_needs_something_to_change() -> None:
    with pytest.raises(DocumentError, match="Say where to move it"):
        run(load(), {"op": "set_position", "visual": CARD})


# --- Arrange and hidden ---------------------------------------------------------------------


def z_order(document: PbirDocument, page: str = PAGE1) -> list[str]:
    visuals = document.pages[page].visuals
    return sorted(visuals, key=lambda n: visuals[n]["position"]["z"])


def test_arrange_to_the_front_takes_a_z_above_every_other_visual() -> None:
    document = load()
    run(document, {"op": "arrange", "visual": COLUMN, "to": "front"})
    assert stored(document, COLUMN)["z"] == 15000
    assert z_order(document)[-1] == COLUMN


def test_arrange_one_step_swaps_z_with_the_neighbour_so_z_stays_unique() -> None:
    document = load()
    outcome = run(document, {"op": "arrange", "visual": COLUMN, "to": "forward"})
    assert (stored(document, COLUMN)["z"], stored(document, COLUMN_2)["z"]) == (7000, 6000)
    assert outcome.summaries == [
        "Moved the 'Total MTD Sales' stacked column chart one step forward."
    ]
    run(document, {"op": "arrange", "visual": COLUMN, "to": "backward"})
    run(document, {"op": "arrange", "visual": COLUMN, "to": "backward"})
    zs = [v["position"]["z"] for v in document.pages[PAGE1].visuals.values()]
    assert len(zs) == len(set(zs))
    assert stored(document, COLUMN)["z"] == 5000


def test_arrange_to_the_back_when_z_zero_is_taken_shifts_only_what_is_below() -> None:
    document = load()
    before = {n: v["position"]["z"] for n, v in document.pages[PAGE1].visuals.items()}
    run(document, {"op": "arrange", "visual": COLUMN, "to": "back"})
    after = {n: v["position"]["z"] for n, v in document.pages[PAGE1].visuals.items()}
    assert z_order(document)[0] == COLUMN and after[COLUMN] == 0
    assert len(set(after.values())) == len(after)
    unchanged = {n for n in before if before[n] == after[n]}
    assert unchanged >= {n for n, z in before.items() if z > before[COLUMN]}  # above it: untouched


def test_arrange_works_among_the_members_of_one_group() -> None:
    document = load("multipage")
    outcome = run(document, {"op": "arrange", "visual": SLICER_A, "to": "front"})
    assert z_order(document, DETAILS).index(SLICER_A) > z_order(document, DETAILS).index(SLICER_B)
    zs = [v["position"]["z"] for v in document.pages[DETAILS].visuals.values()]
    assert len(zs) == len(set(zs))  # unique across the page, not just the group
    assert outcome.summaries == ["Brought the slicer of Store to the front of its group."]
    assert stored(document, GROUP)["z"] == 1000  # the group itself did not move


def test_arrange_at_the_end_already_says_so() -> None:
    document = load("multipage")
    outcome = run(document, {"op": "arrange", "visual": BUTTON, "to": "front"})
    assert outcome.summaries == ["The action button at 1668, 110 is already at the front."]
    assert stored(document, BUTTON)["z"] == 15000


def test_hide_and_show_a_visual_or_a_group() -> None:
    document = load("multipage")
    outcome = run(
        document,
        {"op": "set_visual_hidden", "visual": GROUP, "hidden": True},
        {"op": "set_visual_hidden", "visual": GROUP, "hidden": True},
        {"op": "set_visual_hidden", "visual": GROUP, "hidden": False},
    )
    assert outcome.summaries == [
        "Hid the group 'Header block'.",
        "The group 'Header block' was already hidden.",
        "Showed the group 'Header block'.",
    ]
    assert "isHidden" not in document.visual(GROUP)[1]


# --- the geometry helpers -------------------------------------------------------------------


def test_group_origins_add_up_and_a_box_is_read_and_written_in_page_pixels() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    assert report_edit_geometry.origin(page, SLICER_A) == (80, 120)
    assert report_edit_geometry.origin(page, INNER_GROUP) == (40, 60)
    assert report_edit_geometry.origin(page, GROUP) == (0, 0)
    assert report_edit_geometry.ancestors(page, SLICER_A) == [INNER_GROUP, GROUP]
    assert report_edit_geometry.descendants(page, GROUP) == [
        INNER_GROUP,
        GROUP_CARD,
        SLICER_A,
        SLICER_B,
    ]
    report_edit_geometry.write_box(page, SLICER_B, x=700, y=400, width=210)
    assert stored(document, SLICER_B) == {
        "x": 620,
        "y": 280,
        "z": 4000,
        "height": 60,
        "width": 210,
        "tabOrder": 4000,
    }
    assert page_box(page, SLICER_B) == Box(700, 400, 210, 60)


def test_a_backdrop_shape_does_not_count_as_taking_room() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    run(
        document,
        {
            "op": "add_shape",
            "page": DETAILS,
            "shape": "rectangle",
            "x": 0,
            "y": 0,
            "width": 1280,
            "height": 720,
        },
    )
    (backdrop,) = [
        n for n, v in page.visuals.items() if v.get("visual", {}).get("visualType") == "shape"
    ]
    assert backdrop not in occupied(page)
    assert backdrop in leaf_boxes(page)
    assert IMAGE in occupied(page)


def test_handlers_return_results_with_plain_english_summaries() -> None:
    document = load()
    result = report_edit_layout._apply_set_visual_hidden(
        document, report_edits.SetVisualHidden(visual=CARD, hidden=True), model()
    )
    assert isinstance(result, HandlerResult)
    assert result.summary == "Hid the 'Agents Avg MTD Sales' card."


# --- review fixes ---------------------------------------------------------------------------


def test_adding_a_field_the_well_already_holds_changes_nothing() -> None:
    document = load("multipage")
    _, visual = document.visual(COLUMN)
    before = [p["queryRef"] for p in visual["visual"]["query"]["queryState"]["Y"]["projections"]]
    twice = field("TopN/BottomN/ MTD Sales")
    outcome = run(
        document,
        {
            "op": "set_fields",
            "visual": COLUMN,
            "role": "Y",
            "mode": "add",
            "fields": [twice, twice],
        },
    )
    after = [p["queryRef"] for p in visual["visual"]["query"]["queryState"]["Y"]["projections"]]
    assert after == before == ["# Measures.TopN/BottomN/ MTD Sales"]
    assert outcome.summaries == [
        "TopN/BottomN/ MTD Sales already in Y-axis of the 'Total MTD Sales' stacked column chart."
    ]


def test_add_fields_skips_what_is_there_and_what_the_same_call_lists_twice() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    before = state(visual)["Values"]
    outcome = run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "values",
            "mode": "add",
            "fields": [
                RANK,
                field("EmployeeKey", "DimEmployee"),
                STORE,
                STORE,
                field("Salary", "DimEmployee"),
                field("Salary", "DimEmployee"),
            ],
        },
    )
    assert state(visual)["Values"] == [*before, "StoreName", "Salary"]
    assert outcome.summaries == [
        "Added StoreName and Salary to Columns of the table of Agent ID. "
        "Rank and Agent ID were already there."
    ]


def test_replace_does_not_write_a_field_listed_twice_twice() -> None:
    document = load()
    _, visual = document.visual(TABLE)
    run(
        document,
        {
            "op": "set_fields",
            "visual": TABLE,
            "role": "values",
            "mode": "replace",
            "fields": [STORE, STORE, SALES],
        },
    )
    assert state(visual)["Values"] == ["StoreName", "MTD Sales"]


def test_add_visual_writes_a_field_listed_twice_once() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(
        document,
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "clusteredColumnChart",
            "fields": [CHANNEL, SALES, SALES],
        },
    )
    _, visual = added(document, before)
    refs = [
        p["queryRef"]
        for bucket in visual["visual"]["query"]["queryState"].values()
        for p in bucket["projections"]
    ]
    assert refs == ["DimChannel.ChannelName", "# Measures.Table MTD Sales"]


def test_tied_z_values_are_renumbered_without_touching_another_visuals_z() -> None:
    document = load("multipage")
    document.visual(SLICER_B)[1]["position"]["z"] = 3000  # ties with SLICER_A
    others = {
        n: v["position"]["z"]
        for n, v in document.pages[DETAILS].visuals.items()
        if n not in (SLICER_A, SLICER_B)
    }
    run(document, {"op": "arrange", "visual": SLICER_A, "to": "front"})
    zs = {n: v["position"]["z"] for n, v in document.pages[DETAILS].visuals.items()}
    assert (zs[SLICER_B], zs[SLICER_A]) == (3000, 4000)
    assert {n: z for n, z in zs.items() if n in others} == others
    assert len(set(zs.values())) == len(zs)


def test_tied_z_values_skip_a_value_the_rest_of_the_page_uses() -> None:
    document = load("multipage")
    document.visual(SLICER_B)[1]["position"]["z"] = 3000
    document.visual(GROUP_CARD)[1]["position"]["z"] = 4000  # the next value in line
    run(document, {"op": "arrange", "visual": SLICER_A, "to": "front"})
    zs = {n: v["position"]["z"] for n, v in document.pages[DETAILS].visuals.items()}
    assert (zs[SLICER_B], zs[SLICER_A]) == (3000, 5000)
    assert len(set(zs.values())) == len(zs)


def test_a_group_wider_than_the_target_page_is_refused_by_name() -> None:
    document = load("multipage")
    before = document.visual_names()
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "duplicate_visual", "visual": INNER_GROUP, "page": TOOLTIP})
    assert str(raised.value) == (
        "The group 'Filters' is 420 by 60 and page 'Sales tooltip' is only 320 by 240. "
        "Resize the members individually or duplicate them one by one."
    )
    assert document.visual_names() == before


def test_a_caption_on_two_pages_says_which_page_each_is_on() -> None:
    with pytest.raises(DocumentError) as raised:
        run(load("multipage"), {"op": "set_position", "visual": "Country", "x": 5, "y": 5})
    assert str(raised.value) == (
        "More than one visual is called 'Country': the 'Country' slicer on page '1' and the "
        "'Country' slicer on page 'Details'. Say which one."
    )


@pytest.mark.parametrize(
    ("visual", "what"),
    [(IMAGE, "The image at 1000, 40"), (BUTTON, "The action button at 1668, 110")],
)
def test_a_visual_with_no_fields_cannot_become_a_table(visual: str, what: str) -> None:
    with pytest.raises(DocumentError) as raised:
        run(
            load("multipage"),
            {"op": "change_visual_type", "visual": visual, "visual_type": "tableEx"},
        )
    assert str(raised.value) == (
        f"{what} has no fields to move, so it cannot become a table. Add a table instead."
    )


def test_the_two_card_types_are_told_apart_in_the_receipt() -> None:
    document = load()
    first = run(document, {"op": "change_visual_type", "visual": CARD_BARE, "visual_type": "card"})
    assert first.summaries[0].startswith(
        "Changed the 'Agents with Positive Change %' card into a classic card."
    )
    back = run(
        document, {"op": "change_visual_type", "visual": CARD_BARE, "visual_type": "cardVisual"}
    )
    assert " card into a new card." in back.summaries[0]


def test_a_duplicate_reports_its_lineage_so_it_inherits_the_sources_baseline() -> None:
    from ai_powerbi_analyst.studio import report_edit_validation as validation

    document = load()
    info = model()
    baseline = validation.capture_baseline(document, info)
    before = document.visual_names()
    outcome = run(document, {"op": "duplicate_visual", "visual": CARD_BARE}, info=info)
    (copy,) = document.visual_names() - before
    assert outcome.lineage == {copy: CARD_BARE}
    unfiltered = validation.validate_findings(document, model=info, baseline=baseline)
    inherited = validation.validate_findings(
        document, model=info, baseline=baseline, lineage=outcome.lineage
    )
    assert len(unfiltered) == 8
    assert [(f.code, f.severity) for f in inherited] == [("visuals_overlap", "advisory")]


def test_a_duplicated_group_reports_every_member_in_its_lineage() -> None:
    document = load("multipage")
    before = document.visual_names()
    outcome = run(document, {"op": "duplicate_visual", "visual": INNER_GROUP})
    copies = document.visual_names() - before
    assert len(copies) == 3
    assert set(outcome.lineage) == copies
    assert sorted(outcome.lineage.values()) == sorted([INNER_GROUP, SLICER_A, SLICER_B])


def test_a_new_visual_shrinks_to_a_real_gap_before_covering_one() -> None:
    """The Agents page has no free 400 x 300 spot, but room above its cards for a smaller
    chart (seen live on 2026-09-29: the default-sized chart was put over a card)."""
    document = load()
    page = document.page(PAGE1)
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "clusteredColumnChart",
            "fields": [CHANNEL, SALES],
        },
    )
    _, visual = added(document, before)
    box = Box(**{k: float(visual["position"][k]) for k in ("x", "y", "width", "height")})
    others = [b for name, b in occupied(page, {visual["name"]}).items()]
    assert overlaps(box, others) == []
    assert box.width / box.height == pytest.approx(400 / 300)
    assert not any("covers the least" in w for w in outcome.warnings)


def test_a_size_the_user_gave_is_kept_even_when_the_page_is_full() -> None:
    document = load()
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_visual",
            "page": PAGE1,
            "visual_type": "clusteredColumnChart",
            "fields": [CHANNEL, SALES],
            "width": 600,
            "height": 400,
        },
    )
    _, visual = added(document, before)
    assert (visual["position"]["width"], visual["position"]["height"]) == (600, 400)
    assert any("covers the least" in w for w in outcome.warnings)
