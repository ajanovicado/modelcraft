"""Alignment, distribution, swapping and placing visuals, and adding a text box or a shape.

All positions in operations are page pixels and are resolved for group members (stored relative to
their group). The twin's four cards share a row and its three top slicers share a band; the
multipage fixture adds nested groups on its "Details" page.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from ai_powerbi_analyst.studio import report_edit_align, report_edits
from ai_powerbi_analyst.studio.pbir_document import DocumentError, PbirDocument
from ai_powerbi_analyst.studio.pbir_expr import catalog
from ai_powerbi_analyst.studio.pbix_package import PbixPackage
from ai_powerbi_analyst.studio.report_edit_context import ModelInfo, VisibleIn, visible_caption
from ai_powerbi_analyst.studio.report_edit_geometry import page_box

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "imported"
STUDIO = Path(report_edits.__file__).parent

PAGE1 = "ReportSection8454c812b60b8bb83b99"
DETAILS = "ReportSection3f1c0d5e7a9b2c4d6e8f"

CARD_A = "fb4c5aa34e0b084b62b9"  # cards in one row: y 154, 354.33 x 313
CARD_B = "2202e311b1493069e4eb"
CARD_C = "9479353528b0eb22b958"
CARD_D = "71a164765d9c63200302"
COLUMN = "b404a461ede7604a2774"  # (174.32, 645.95), 360.81 x 224.32
COLUMN_2 = "da32cdbb0090e885649b"
SLICER_TOP = "5b0dbccd61c6c4adb415"  # the three slicers of the top band, y 40
SLICER_MID = "54be5cf0783e66471540"
SLICER_LEFT = "b85aa81a8b5c60030d13"  # 210.45 x 61
GROUP = "40c896eda14bfa8f6a97"
INNER_GROUP = "7799291338ca3548d7d5"
SLICER_A = "04ba32659627f6e8c7d7"  # page (120, 180), 200 x 60
SLICER_B = "2b5b8e3a7ed15138a983"  # page (340, 180)
GROUP_CARD = "dfb4645ec076d1209ebe"  # page (80, 200), 300 x 120
IMAGE = "81ab76274a5b4e9e62da"  # (1000, 40), 120 x 120
BUTTON = "d6c05c8319906941fe02"  # Details: (40, 620), 200 x 40


def load(name: str = "structure-twin") -> PbirDocument:
    return PbirDocument(PbixPackage(FIXTURES / f"{name}.pbix"))


def model() -> ModelInfo:
    return ModelInfo.from_report([], [{"Name": "Rank", "TableName": "# Measures"}])


def run(document: PbirDocument, *operations: dict[str, Any]) -> report_edits.EditOutcome:
    parsed = report_edits.parse_operations(list(operations))
    return report_edits.apply_operations(document, list(parsed), model())


def box(document: PbirDocument, name: str) -> tuple[float, float, float, float]:
    page, _ = document.visual(name)
    found = page_box(page, name)
    return found.x, found.y, found.width, found.height


def close(actual: tuple[float, ...], expected: tuple[float, ...]) -> None:
    assert actual == pytest.approx(expected, abs=1e-6)


def stored(document: PbirDocument, name: str) -> dict[str, Any]:
    position: dict[str, Any] = document.visual(name)[1]["position"]
    return position


def new_visual(document: PbirDocument, before: set[str]) -> dict[str, Any]:
    (name,) = document.visual_names() - before
    return document.visual(name)[1]


def shapes(
    document: PbirDocument, page: str, *boxes: tuple[float, float, float, float]
) -> list[str]:
    """Rectangles at the given (x, y, width, height), for arithmetic on round numbers."""
    names = []
    for x, y, width, height in boxes:
        before = document.visual_names()
        run(
            document,
            {"op": "add_shape", "page": page, "x": x, "y": y, "width": width, "height": height},
        )
        names.append(next(iter(document.visual_names() - before)))
    return names


# --- AlignVisuals ---------------------------------------------------------------------------

SLICERS = [SLICER_LEFT, SLICER_MID, SLICER_TOP]


@pytest.mark.parametrize(
    ("edge", "expected"),
    [
        ("left", [(1120.8955223880596, 40), (1120.8955223880596, 40), (1120.8955223880596, 40)]),
        ("right", [(1668, 40), (1668, 40), (1668, 40)]),
        ("top", [(1120.8955223880596, 40), (1394.4477611940297, 40), (1668, 40)]),
        ("bottom", [(1120.8955223880596, 40), (1394.4477611940297, 41), (1668, 41)]),
        ("center_v", [(1120.8955223880596, 40), (1394.4477611940297, 40.5), (1668, 40.5)]),
        (
            "center_h",
            [(1394.4477611940297, 40), (1394.4477611940297, 40), (1394.4477611940297, 40)],
        ),
    ],
)
def test_align_to_the_selection_uses_its_outermost_edge_or_its_middle(
    edge: str, expected: list[tuple[float, float]]
) -> None:
    document = load()
    run(document, {"op": "align_visuals", "visuals": SLICERS, "edge": edge})
    found = [(box(document, n)[0], box(document, n)[1]) for n in SLICERS]
    for got, want in zip(found, expected, strict=True):
        close(got, want)


@pytest.mark.parametrize(
    ("edge", "expected"),
    [
        ("left", (0, 40)),
        ("right", (1920 - 210.44776119402985, 40)),
        ("top", (1668, 0)),
        ("bottom", (1668, 1200 - 60)),
        ("center_h", ((1920 - 210.44776119402985) / 2, 40)),
        ("center_v", (1668, (1200 - 60) / 2)),
    ],
)
def test_align_to_the_page_uses_the_page_edges(edge: str, expected: tuple[float, float]) -> None:
    document = load()
    outcome = run(
        document, {"op": "align_visuals", "visuals": [SLICER_TOP], "edge": edge, "to": "page"}
    )
    close(box(document, SLICER_TOP)[:2], expected)
    assert len(outcome.summaries) == 1


def test_the_summary_says_what_was_lined_up() -> None:
    document = load()
    outcome = run(document, {"op": "align_visuals", "visuals": [CARD_A, COLUMN], "edge": "left"})
    assert outcome.summaries == [
        "Lined up the left edges of the 'Agents with Positive Change %' card and the "
        "'Total MTD Sales' stacked column chart."
    ]
    outcome = run(
        document, {"op": "align_visuals", "visuals": [CARD_A], "edge": "top", "to": "page"}
    )
    assert outcome.summaries == [
        "Aligned the 'Agents with Positive Change %' card to the top of the page."
    ]
    outcome = run(
        document, {"op": "align_visuals", "visuals": [CARD_A], "edge": "top", "to": "page"}
    )
    assert outcome.summaries[0].endswith("the top of the page: they were already aligned.")


def test_aligning_group_members_writes_positions_relative_to_the_group() -> None:
    document = load("multipage")
    card_before = dict(stored(document, GROUP_CARD))
    run(
        document,
        {"op": "align_visuals", "visuals": [SLICER_A, SLICER_B, GROUP_CARD], "edge": "left"},
    )
    # Page x 80 is the leftmost; the slicers sit in a group at (80, 120) in page pixels.
    assert box(document, SLICER_A)[0] == box(document, SLICER_B)[0] == 80
    assert (stored(document, SLICER_A)["x"], stored(document, SLICER_B)["x"]) == (0, 0)
    assert stored(document, GROUP_CARD) == card_before  # already there: not touched


def test_aligning_a_group_moves_only_the_group() -> None:
    document = load("multipage")
    member = dict(stored(document, SLICER_A))
    run(document, {"op": "align_visuals", "visuals": [GROUP, IMAGE], "edge": "top"})
    assert stored(document, GROUP)["y"] == 40 and stored(document, IMAGE)["y"] == 40
    assert stored(document, SLICER_A) == member


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {"op": "align_visuals", "visuals": [CARD_A], "edge": "left"},
            "Pick at least two visuals to line up with each other, or align this one to the page",
        ),
        (
            {"op": "align_visuals", "visuals": [GROUP, SLICER_A], "edge": "left"},
            "The slicer of Store is inside the group 'Header block'. Align the group or the "
            "visuals in it, not both.",
        ),
        (
            {"op": "align_visuals", "visuals": [SLICER_A, "Nowhere"], "edge": "left"},
            None,
        ),
    ],
)
def test_alignments_that_cannot_be_done_are_refused(
    operation: dict[str, Any], message: str | None
) -> None:
    with pytest.raises(DocumentError) as raised:
        run(load("multipage"), operation)
    if message:
        assert str(raised.value) == message
    else:
        assert str(raised.value).startswith("There is no visual 'Nowhere' in this report.")


def test_visuals_on_two_pages_cannot_be_aligned_together() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "align_visuals", "visuals": [CARD_A, SLICER_A], "edge": "left"})
    assert str(raised.value) == (
        "Align works within one page, but the slicer of Store is on page 'Details' and the others "
        "are on '1'."
    )


def test_an_alignment_that_creates_an_overlap_says_so() -> None:
    document = load()
    outcome = run(document, {"op": "align_visuals", "visuals": [CARD_A, COLUMN], "edge": "top"})
    assert outcome.warnings == [
        "The 'Total MTD Sales' stacked column chart now overlaps the 'Agents with Positive "
        "Change %' card."
    ]


# --- DistributeVisuals ---------------------------------------------------------------------


def test_distribute_keeps_the_outer_two_and_evens_the_gaps_between() -> None:
    document = load()
    run(
        document,
        {"op": "set_position", "visual": CARD_B, "x": 700},
        {"op": "set_position", "visual": CARD_C, "x": 1200},
    )
    outcome = run(
        document,
        {
            "op": "distribute_visuals",
            "visuals": [CARD_D, CARD_B, CARD_A, CARD_C],
            "axis": "horizontal",
        },
    )
    xs = [box(document, n)[0] for n in (CARD_A, CARD_B, CARD_C, CARD_D)]
    close(tuple(xs), (173, 612, 1051, 1490))  # the row's original even spacing
    assert outcome.summaries[0].endswith("evenly from left to right, 84.7 px apart.")
    assert outcome.warnings == []


def test_distribute_vertically_on_round_numbers() -> None:
    document = load()
    names = shapes(document, PAGE1, (0, 0, 100, 100), (0, 150, 100, 100), (0, 600, 100, 100))
    outcome = run(document, {"op": "distribute_visuals", "visuals": names, "axis": "vertical"})
    assert [box(document, n)[1] for n in names] == [0, 300, 600]
    assert outcome.summaries[0].endswith("evenly from top to bottom, 200 px apart.")


def test_distribute_sorts_by_position_not_by_the_order_given() -> None:
    document = load()
    names = shapes(document, PAGE1, (0, 0, 100, 100), (50, 0, 100, 100), (700, 0, 100, 100))
    run(
        document,
        {
            "op": "distribute_visuals",
            "visuals": [names[2], names[0], names[1]],
            "axis": "horizontal",
        },
    )
    assert [box(document, n)[0] for n in names] == [0, 350, 700]


def test_distribute_that_cannot_leave_a_gap_warns_that_they_overlap() -> None:
    document = load()
    names = shapes(document, PAGE1, (0, 0, 300, 100), (100, 0, 300, 100), (350, 0, 300, 100))
    outcome = run(document, {"op": "distribute_visuals", "visuals": names, "axis": "horizontal"})
    assert [box(document, n)[0] for n in names] == [0, 175, 350]  # gap -125
    assert (
        outcome.warnings[0]
        == "They are wider than the space they were spread over, so they overlap."
    )


def test_distribute_needs_three_visuals() -> None:
    with pytest.raises(DocumentError) as raised:
        run(load(), {"op": "distribute_visuals", "visuals": [CARD_A, CARD_B], "axis": "horizontal"})
    assert str(raised.value) == (
        "Spacing evenly needs at least three visuals: the two outermost stay where they are and "
        "the ones between are spaced out"
    )


# --- SwapVisuals ----------------------------------------------------------------------------


def test_swap_exchanges_positions_and_sizes() -> None:
    document = load()
    first, second = box(document, CARD_A), box(document, COLUMN)
    outcome = run(document, {"op": "swap_visuals", "a": CARD_A, "b": COLUMN})
    close(box(document, CARD_A), second)
    close(box(document, COLUMN), first)
    assert outcome.summaries == [
        "Swapped the 'Agents with Positive Change %' card and the 'Total MTD Sales' stacked "
        "column chart."
    ]


def test_swapping_a_visual_with_itself_or_its_group_is_refused() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError, match="Pick two different visuals to swap"):
        run(document, {"op": "swap_visuals", "a": CARD_A, "b": CARD_A})
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "swap_visuals", "a": GROUP, "b": SLICER_A})
    assert str(raised.value) == (
        "The slicer of Store is inside the group 'Header block'. Swap the group or the visuals "
        "in it, not both."
    )


def test_a_group_swaps_places_only_with_a_visual_of_its_own_size() -> None:
    document = load("multipage")
    with pytest.raises(DocumentError) as raised:
        run(document, {"op": "swap_visuals", "a": GROUP, "b": IMAGE})
    assert str(raised.value) == (
        "A group cannot be resized here, so it can only swap places with a visual of the same "
        "size. Resize the members individually, or ungroup in Power BI Desktop."
    )
    run(
        document,
        {"op": "set_position", "visual": IMAGE, "x": 800, "y": 40, "width": 420, "height": 200},
    )
    member = box(document, SLICER_A)
    run(document, {"op": "swap_visuals", "a": GROUP, "b": IMAGE})
    assert box(document, GROUP) == (800, 40, 420, 200) and box(document, IMAGE) == (
        40,
        60,
        420,
        200,
    )
    assert box(document, SLICER_A)[0] == member[0] + 760  # the members went with the group


# --- PlaceVisual ----------------------------------------------------------------------------


def test_place_beside_above_and_below_with_a_gap() -> None:
    document = load()
    anchor = box(document, CARD_B)  # (612, 154, 354.33, 313)
    run(
        document,
        {
            "op": "place_visual",
            "visual": COLUMN,
            "relative_to": CARD_B,
            "side": "right_of",
            "gap": 20,
        },
    )
    close(box(document, COLUMN)[:2], (anchor[0] + anchor[2] + 20, anchor[1]))
    run(
        document,
        {"op": "place_visual", "visual": COLUMN, "relative_to": CARD_B, "side": "below", "gap": 15},
    )
    close(box(document, COLUMN)[:2], (anchor[0], anchor[1] + anchor[3] + 15))
    lower = box(document, COLUMN_2)
    run(
        document, {"op": "place_visual", "visual": COLUMN, "relative_to": COLUMN_2, "side": "above"}
    )
    close(box(document, COLUMN)[:2], (lower[0], lower[1] - 10 - box(document, COLUMN)[3]))
    run(
        document,
        {
            "op": "place_visual",
            "visual": COLUMN,
            "relative_to": CARD_B,
            "side": "left_of",
            "gap": 0,
        },
    )
    close(box(document, COLUMN)[:2], (anchor[0] - box(document, COLUMN)[2], anchor[1]))


def test_align_with_lines_up_by_the_edge_that_keeps_them_apart() -> None:
    document = load()
    # COLUMN sits below the cards: they are stacked, so the left edges line up.
    run(
        document,
        {"op": "place_visual", "visual": COLUMN, "relative_to": CARD_B, "side": "align_with"},
    )
    assert box(document, COLUMN)[0] == box(document, CARD_B)[0]
    # Side by side: the tops line up.
    run(
        document,
        {
            "op": "place_visual",
            "visual": CARD_D,
            "relative_to": CARD_A,
            "side": "align_with",
            "edge": "bottom",
        },
    )
    close(
        (box(document, CARD_D)[1] + box(document, CARD_D)[3],),
        (box(document, CARD_A)[1] + box(document, CARD_A)[3],),
    )


def test_place_next_to_the_page_edge_is_kept_on_the_page_and_says_so() -> None:
    document = load()
    outcome = run(
        document,
        {"op": "place_visual", "visual": COLUMN, "relative_to": CARD_D, "side": "right_of"},
    )
    x, _, width, _ = box(document, COLUMN)
    assert x + width == pytest.approx(1920)
    assert (
        outcome.warnings[0]
        == "There is not enough room on the page there, so it sits against the page edge."
    )


def test_place_relative_to_itself_or_its_own_group_is_refused() -> None:
    with pytest.raises(DocumentError, match="A visual cannot be placed relative to itself"):
        run(
            load(), {"op": "place_visual", "visual": CARD_A, "relative_to": CARD_A, "side": "below"}
        )
    with pytest.raises(DocumentError, match="not both"):
        run(
            load("multipage"),
            {"op": "place_visual", "visual": SLICER_A, "relative_to": GROUP, "side": "below"},
        )


def test_placing_a_group_member_uses_page_pixels_for_both() -> None:
    document = load("multipage")
    run(
        document,
        {
            "op": "place_visual",
            "visual": SLICER_A,
            "relative_to": IMAGE,
            "side": "below",
            "gap": 10,
        },
    )
    assert box(document, SLICER_A)[:2] == (1000, 170)  # image bottom (160) + 10, image left
    assert (stored(document, SLICER_A)["x"], stored(document, SLICER_A)["y"]) == (920, 50)


# --- AddTextbox -----------------------------------------------------------------------------


def test_a_text_box_carries_its_text_as_paragraphs_of_text_runs() -> None:
    document = load("multipage")
    schema = document.schema("visualContainer")
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_textbox",
            "page": DETAILS,
            "text": "Sales review\nQ3",
            "x": 10,
            "y": 10,
            "width": 300,
            "height": 60,
            "font_family": "Segoe UI",
            "font_size": 18,
            "bold": True,
            "color": "#112233",
        },
    )
    visual = new_visual(document, before)
    style = {"fontFamily": "Segoe UI", "fontSize": "18pt", "color": "#112233", "fontWeight": "bold"}
    assert visual == {
        "$schema": schema,
        "name": visual["name"],
        "position": {"x": 10, "y": 10, "z": 8000, "height": 60, "width": 300, "tabOrder": 8000},
        "visual": {
            "visualType": "textbox",
            "drillFilterOtherVisuals": True,
            "objects": {
                "general": [
                    {
                        "properties": {
                            "paragraphs": [
                                {"textRuns": [{"value": "Sales review", "textStyle": style}]},
                                {"textRuns": [{"value": "Q3", "textStyle": style}]},
                            ]
                        }
                    }
                ]
            },
        },
    }
    assert visible_caption(visual) == "Sales review Q3"
    assert outcome.summaries == ["Added a text box saying 'Sales review Q3' to page 'Details'."]


def test_plain_text_has_no_style_and_a_theme_color_is_written_as_hex() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(document, {"op": "add_textbox", "page": DETAILS, "text": "Just text"})
    visual = new_visual(document, before)
    assert visual["visual"]["objects"]["general"][0]["properties"]["paragraphs"] == [
        {"textRuns": [{"value": "Just text"}]}
    ]
    assert (visual["position"]["width"], visual["position"]["height"]) == (
        300,
        60,
    )  # the type's size
    before = document.visual_names()
    run(document, {"op": "add_textbox", "page": DETAILS, "text": "Green", "color": {"theme": 2}})
    visual = new_visual(document, before)
    (run_,) = visual["visual"]["objects"]["general"][0]["properties"]["paragraphs"][0]["textRuns"]
    assert run_["textStyle"] == {"color": document.palette()[2]}


def test_a_text_box_is_placed_in_free_space_and_may_be_laid_over_a_visual_on_request() -> None:
    document = load("multipage")
    page = document.pages[DETAILS]
    before = document.visual_names()
    outcome = run(document, {"op": "add_textbox", "page": DETAILS, "text": "Note"})
    visual = new_visual(document, before)
    from ai_powerbi_analyst.studio.report_edit_geometry import occupied
    from ai_powerbi_analyst.studio.report_roles import overlaps

    assert (
        overlaps(
            page_box(page, visual["name"]),
            [b for n, b in occupied(page).items() if n != visual["name"]],
        )
        == []
    )
    assert outcome.warnings == []
    # A caption laid over a chart is what text boxes are for: no overlap advisory.
    outcome = run(
        document,
        {
            "op": "add_textbox",
            "page": DETAILS,
            "text": "Over",
            "x": 120,
            "y": 180,
            "width": 100,
            "height": 20,
        },
    )
    assert outcome.warnings == []


def test_text_with_control_characters_is_refused() -> None:
    with pytest.raises(DocumentError, match="control characters"):
        run(load(), {"op": "add_textbox", "page": PAGE1, "text": "bad\x00text"})


# --- AddShape -------------------------------------------------------------------------------


def literal(text: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": text}}}


def test_a_shape_carries_the_default_state_selector_on_everything_it_writes() -> None:
    document = load("multipage")
    schema = document.schema("visualContainer")
    before = document.visual_names()
    outcome = run(
        document,
        {
            "op": "add_shape",
            "page": DETAILS,
            "shape": "oval",
            "fill": "#ff0000",
            "outline": {"theme": 3, "percent": -0.2},
            "x": 700,
            "y": 400,
            "width": 120,
            "height": 80,
        },
    )
    visual = new_visual(document, before)
    default = {"id": "default"}
    assert visual == {
        "$schema": schema,
        "name": visual["name"],
        "position": {"x": 700, "y": 400, "z": 8000, "height": 80, "width": 120, "tabOrder": 8000},
        "visual": {
            "visualType": "shape",
            "drillFilterOtherVisuals": True,
            "objects": {
                "shape": [{"properties": {"tileShape": literal("'oval'")}, "selector": default}],
                "fill": [
                    {
                        "properties": {
                            "show": literal("true"),
                            "fillColor": {"solid": {"color": literal("'#FF0000'")}},
                        },
                        "selector": default,
                    }
                ],
                "outline": [
                    {
                        "properties": {
                            "show": literal("true"),
                            "lineColor": {
                                "solid": {
                                    "color": {
                                        "expr": {"ThemeDataColor": {"ColorId": 3, "Percent": -0.2}}
                                    }
                                }
                            },
                        },
                        "selector": default,
                    }
                ],
            },
        },
    }
    assert outcome.summaries == ["Added an oval shape to page 'Details'."]
    assert outcome.visible_in == []  # Studio draws an oval


def test_a_shape_with_no_colors_writes_only_its_geometry() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(document, {"op": "add_shape", "page": DETAILS, "x": 0, "y": 0})
    visual = new_visual(document, before)
    assert visual["visual"]["objects"] == {
        "shape": [
            {"properties": {"tileShape": literal("'rectangle'")}, "selector": {"id": "default"}}
        ]
    }
    assert (visual["position"]["width"], visual["position"]["height"]) == (200, 100)


def test_a_line_is_thin_and_a_shape_studio_cannot_draw_says_so() -> None:
    document = load("multipage")
    before = document.visual_names()
    run(document, {"op": "add_shape", "page": DETAILS, "shape": "line", "x": 0, "y": 700})
    assert (
        new_visual(document, before)["position"]["width"],
        new_visual(document, before)["position"]["height"],
    ) == (200, 10)
    outcome = run(
        document,
        {"op": "add_shape", "page": DETAILS, "shape": "speechbubbleRectangle", "x": 0, "y": 0},
    )
    assert outcome.summaries == ["Added a speechbubble rectangle shape to page 'Details'."]
    assert outcome.visible_in == [
        VisibleIn(
            "add_shape",
            "powerbi_only",
            "Studio draws the speechbubble rectangle shape as a plain rectangle; Power BI "
            "draws it properly.",
        )
    ]


def test_the_shape_names_are_the_catalogs() -> None:
    allowed = report_edit_align.AddShape.model_fields["shape"].annotation
    names = set(allowed.__args__)  # type: ignore[union-attr]
    assert names == set(
        catalog()["visualTypes"]["shape"]["objects"]["shape"]["tileShape"]["values"]
    )
    with pytest.raises(DocumentError, match="'shape' is not valid"):
        report_edits.parse_operations([{"op": "add_shape", "page": "p", "shape": "blob"}])


def test_adding_a_text_box_or_shape_introduces_no_validation_problem() -> None:
    document = load("multipage")
    before = report_edits.validate_document(document)
    run(
        document,
        {"op": "add_textbox", "page": DETAILS, "text": "Hello"},
        {"op": "add_shape", "page": DETAILS, "fill": "#00AA00"},
    )
    assert report_edits.validate_document(document) == before


# --- wiring ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (
            {"op": "align_visuals", "visuals": ["a"], "edge": "center_h", "to": "page"},
            {"op": "align_visuals", "visuals": ["a"], "edge": "center_h", "to": "page"},
        ),
        (
            {"op": "align_visuals", "visuals": ["a", "b"], "edge": "top"},
            {"op": "align_visuals", "visuals": ["a", "b"], "edge": "top", "to": "selection"},
        ),
        (
            {"op": "distribute_visuals", "visuals": ["a", "b", "c"], "axis": "vertical"},
            {"op": "distribute_visuals", "visuals": ["a", "b", "c"], "axis": "vertical"},
        ),
        ({"op": "swap_visuals", "a": "a", "b": "b"}, {"op": "swap_visuals", "a": "a", "b": "b"}),
        (
            {
                "op": "place_visual",
                "visual": "a",
                "relative_to": "b",
                "side": "align_with",
                "gap": 4,
                "edge": "top",
            },
            {
                "op": "place_visual",
                "visual": "a",
                "relative_to": "b",
                "side": "align_with",
                "gap": 4,
                "edge": "top",
            },
        ),
        (
            {"op": "place_visual", "visual": "a", "relative_to": "b", "side": "below"},
            {"op": "place_visual", "visual": "a", "relative_to": "b", "side": "below", "gap": 10},
        ),
        (
            {"op": "add_textbox", "page": "p", "text": "Hi", "font_size": 12},
            {"op": "add_textbox", "page": "p", "text": "Hi", "font_size": 12},
        ),
        (
            {"op": "add_shape", "page": "p", "shape": "pill", "fill": "#123456"},
            {"op": "add_shape", "page": "p", "shape": "pill", "fill": "#123456"},
        ),
        ({"op": "add_shape", "page": "p"}, {"op": "add_shape", "page": "p", "shape": "rectangle"}),
    ],
)
def test_every_new_operation_parses_with_its_defaults(
    step: dict[str, Any], expected: dict[str, Any]
) -> None:
    (parsed,) = report_edits.parse_operations([step])
    assert parsed.model_dump(exclude_none=True) == expected


def test_a_bad_edge_is_explained_in_plain_english() -> None:
    with pytest.raises(DocumentError) as raised:
        report_edits.parse_operations([{"op": "align_visuals", "visuals": ["a"], "edge": "middle"}])
    assert str(raised.value).startswith(
        "Step 1 (align_visuals): 'edge' is not valid: input should be 'left'"
    )


def test_the_new_modules_import_in_one_direction() -> None:
    def edit_imports(module: str) -> set[str]:
        tree = ast.parse((STUDIO / f"{module}.py").read_text(encoding="utf-8"))
        return {
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
            if node.module.startswith("report_edit")
        }

    assert edit_imports("report_edit_geometry") == {"report_edit_context"}
    assert edit_imports("report_edit_align") == {
        "report_edit_context",
        "report_edit_geometry",
        "report_edit_ops",
    }
    assert edit_imports("report_edit_layout") == {
        "report_edit_align",
        "report_edit_context",
        "report_edit_geometry",
        "report_edit_ops",
    }
