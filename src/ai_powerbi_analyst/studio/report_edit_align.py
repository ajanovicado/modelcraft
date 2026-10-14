"""Arranging visuals against each other, and adding the two visuals that hold no fields.

Operations here take absolute values in page pixels and resolve everything (a group member's
position, a page's size) themselves, so a request like "line these three up" or "put the map
below the table" needs no arithmetic from the caller:

- ``AlignVisuals``, ``DistributeVisuals``, ``SwapVisuals``, ``PlaceVisual``: move visuals
  relative to each other or to the page. Moving a group moves only the group's own position
  (its members are stored relative to it); a group is never resized (its members' scaling
  has not been measured in Power BI Desktop).
- ``AddTextbox`` and ``AddShape``: a text box carries its text as paragraphs of text runs,
  and a shape's fill, outline and geometry are written under the ``{"id": "default"}``
  selector: Desktop draws a ``shape`` whose fill has no state selector in the default color,
  so a fill written without one would be ignored (measured 2026-09-28).

Every operation ends with advisory notes (a visual past the page edge, one that now covers
another). They are warnings on the result and never stop the edit.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

from . import pbir_expr as expr
from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_edit_context import Handler, HandlerResult, ModelInfo, VisibleIn, _page_bounds, _set
from .report_edit_geometry import (
    CROWDED,
    TOLERANCE,
    advisories,
    ancestors,
    attach,
    choose_placement,
    clamp,
    describe,
    is_group,
    join_names,
    leaf_boxes,
    move_box,
    new_visual,
    page_box,
    resolve_page,
    resolve_visual,
    sentence,
    show,
)
from .report_edit_ops import ColorValue, ThemeColorRef, _color_arg, operation_classes
from .report_roles import Box, default_size
from .report_tokens import DRAWN_SHAPES

Edge = Literal["left", "right", "top", "bottom", "center_h", "center_v"]
# The tileShape values Desktop has (pbir_catalog.json, shape.shape.tileShape); a test keeps
# this list equal to the catalog's.
ShapeName = Literal[
    "arrow",
    "arrowChevron",
    "arrowPentagon",
    "heart",
    "hexagon",
    "line",
    "octagon",
    "oval",
    "parallelogram",
    "pentagon",
    "pill",
    "rectangle",
    "rectangleRounded",
    "rectangleRoundedByPixel",
    "tabCutCorner",
    "tabCutTopCorners",
    "tabCutTopCornersByPixel",
    "tabRoundCorner",
    "tabRoundTopCorners",
    "speechbubbleRectangle",
    "trapezoid",
    "triangleIsoc",
    "triangleRight",
]


class AlignVisuals(ClosedModel):
    """Line visuals up with each other (``to`` "selection": the outermost edge or the middle
    of the group of them) or with the page."""

    op: Literal["align_visuals"] = "align_visuals"
    visuals: list[str] = Field(min_length=1, max_length=50)
    edge: Edge
    to: Literal["selection", "page"] = "selection"


class DistributeVisuals(ClosedModel):
    """Space visuals evenly: the outer two stay, the ones between get equal gaps."""

    op: Literal["distribute_visuals"] = "distribute_visuals"
    visuals: list[str] = Field(min_length=2, max_length=50)
    axis: Literal["horizontal", "vertical"]


class SwapVisuals(ClosedModel):
    """Exchange two visuals' positions and sizes."""

    op: Literal["swap_visuals"] = "swap_visuals"
    a: str
    b: str


class PlaceVisual(ClosedModel):
    """Put a visual beside, above or below another one, ``gap`` pixels away (beside and
    above/below keep the two aligned by their top or left edge); ``align_with`` lines the
    visual up with the other one by ``edge``, by default the edge that keeps them apart."""

    op: Literal["place_visual"] = "place_visual"
    visual: str
    relative_to: str
    side: Literal["left_of", "right_of", "above", "below", "align_with"]
    gap: float = Field(default=10, ge=0, le=2000)
    edge: Edge | None = None


class AddTextbox(ClosedModel):
    op: Literal["add_textbox"] = "add_textbox"
    page: str
    text: str = Field(min_length=1, max_length=2000)
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=10, le=20000)
    height: float | None = Field(default=None, ge=10, le=20000)
    font_family: str | None = Field(default=None, max_length=100)
    font_size: float | None = Field(default=None, ge=6, le=72)
    color: ColorValue | None = None
    bold: bool | None = None


class AddShape(ClosedModel):
    op: Literal["add_shape"] = "add_shape"
    page: str
    shape: ShapeName = "rectangle"
    fill: ColorValue | None = None
    outline: ColorValue | None = None
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=10, le=20000)
    height: float | None = Field(default=None, ge=10, le=20000)


AlignOperation = (
    AlignVisuals | DistributeVisuals | SwapVisuals | PlaceVisual | AddTextbox | AddShape
)

_EDGE_WORDS: dict[str, str] = {
    "left": "left edges",
    "right": "right edges",
    "top": "tops",
    "bottom": "bottoms",
    "center_h": "horizontal centers",
    "center_v": "vertical centers",
}
_PAGE_EDGE_WORDS: dict[str, str] = {
    "left": "the left edge of the page",
    "right": "the right edge of the page",
    "top": "the top of the page",
    "bottom": "the bottom of the page",
    "center_h": "the horizontal middle of the page",
    "center_v": "the vertical middle of the page",
}


# --- picking the visuals an operation moves ------------------------------------------------


def _selection(
    document: PbirDocument, refs: Sequence[str], action: str
) -> tuple[PageDoc, list[str]]:
    """The named visuals (or groups) on one page, each once. A visual and a group it sits in
    cannot both be moved: the member would move twice."""
    page: PageDoc | None = None
    names: list[str] = []
    for ref in refs:
        found_page, visual = resolve_visual(document, ref)
        if page is not None and found_page is not page:
            raise DocumentError(
                f"{action} works within one page, but {describe(visual, found_page)} is on page "
                f"'{found_page.display_name}' and the others are on '{page.display_name}'."
            )
        page = found_page
        name = str(visual["name"])
        if name not in names:
            names.append(name)
    assert page is not None
    _no_nesting(page, names, action)
    return page, names


def _no_nesting(page: PageDoc, names: Sequence[str], action: str) -> None:
    for name in names:
        for group in ancestors(page, name):
            if group in names:
                raise DocumentError(
                    f"{sentence(describe(page.visuals[name], page))} is inside "
                    f"{describe(page.visuals[group], page)}. {action} the group or the visuals "
                    "in it, not both."
                )


def _done(
    page: PageDoc, summary: str, moved: Sequence[str], before: Mapping[str, Box]
) -> HandlerResult:
    return HandlerResult(summary, warnings=advisories(page, moved, before=before))


# --- align, distribute, swap, place ----------------------------------------------------------


def _apply_align_visuals(
    document: PbirDocument, op: AlignVisuals, model: ModelInfo
) -> HandlerResult:
    page, names = _selection(document, op.visuals, "Align")
    if op.to == "selection" and len(names) < 2:
        raise DocumentError(
            "Pick at least two visuals to line up with each other, or align this one to the page"
        )
    before = leaf_boxes(page)
    boxes = {n: page_box(page, n) for n in names}
    page_width, page_height = _page_bounds(page)
    if op.to == "page":
        left, top, right, bottom = 0.0, 0.0, page_width, page_height
    else:
        left = min(b.x for b in boxes.values())
        top = min(b.y for b in boxes.values())
        right = max(b.right for b in boxes.values())
        bottom = max(b.bottom for b in boxes.values())
    moved: list[str] = []
    for name, box in boxes.items():
        x = y = None
        if op.edge == "left":
            x = left
        elif op.edge == "right":
            x = right - box.width
        elif op.edge == "center_h":
            x = (left + right) / 2 - box.width / 2
        elif op.edge == "top":
            y = top
        elif op.edge == "bottom":
            y = bottom - box.height
        else:
            y = (top + bottom) / 2 - box.height / 2
        if move_box(document, page, name, x=x, y=y):
            moved.append(name)
    labels = [describe(page.visuals[n], page) for n in names]
    if op.to == "page":
        summary = f"Aligned {join_names(labels)} to {_PAGE_EDGE_WORDS[op.edge]}."
    else:
        summary = f"Lined up the {_EDGE_WORDS[op.edge]} of {join_names(labels)}."
    if not moved:
        summary = f"{summary[:-1]}: they were already aligned."
    return _done(page, summary, moved, before)


def _apply_distribute_visuals(
    document: PbirDocument, op: DistributeVisuals, model: ModelInfo
) -> HandlerResult:
    page, names = _selection(document, op.visuals, "Distribute")
    if len(names) < 3:
        raise DocumentError(
            "Spacing evenly needs at least three visuals: the two outermost stay where they "
            "are and the ones between are spaced out"
        )
    before = leaf_boxes(page)
    horizontal = op.axis == "horizontal"
    boxes = {n: page_box(page, n) for n in names}

    def start(box: Box) -> float:
        return box.x if horizontal else box.y

    def size(box: Box) -> float:
        return box.width if horizontal else box.height

    order = sorted(names, key=lambda n: start(boxes[n]) + size(boxes[n]) / 2)
    first, last = boxes[order[0]], boxes[order[-1]]
    span = start(last) + size(last) - start(first)
    gap = (span - sum(size(boxes[n]) for n in order)) / (len(order) - 1)
    cursor = start(first)
    moved: list[str] = []
    for name in order:
        target = {"x": cursor} if horizontal else {"y": cursor}
        if move_box(document, page, name, **target):
            moved.append(name)
        cursor += size(boxes[name]) + gap
    labels = [describe(page.visuals[n], page) for n in order]
    where = "left to right" if horizontal else "top to bottom"
    summary = f"Spaced {join_names(labels)} evenly from {where}, {show(gap)} px apart."
    warnings = advisories(page, moved, before=before)
    if gap < 0:
        warnings.insert(0, "They are wider than the space they were spread over, so they overlap.")
    return HandlerResult(summary, warnings=warnings)


def _apply_swap_visuals(document: PbirDocument, op: SwapVisuals, model: ModelInfo) -> HandlerResult:
    page, names = _selection(document, [op.a, op.b], "Swap")
    if len(names) != 2:
        raise DocumentError("Pick two different visuals to swap")
    first, second = names
    box_a, box_b = page_box(page, first), page_box(page, second)
    if (is_group(page.visuals[first]) or is_group(page.visuals[second])) and (
        abs(box_a.width - box_b.width) > TOLERANCE or abs(box_a.height - box_b.height) > TOLERANCE
    ):
        raise DocumentError(
            "A group cannot be resized here, so it can only swap places with a visual of the "
            "same size. Resize the members individually, or ungroup in Power BI Desktop."
        )
    before = leaf_boxes(page)
    for name, other in ((first, box_b), (second, box_a)):
        size = (
            {} if is_group(page.visuals[name]) else {"width": other.width, "height": other.height}
        )
        move_box(document, page, name, x=other.x, y=other.y, **size)
    summary = (
        f"Swapped {describe(page.visuals[first], page)} and {describe(page.visuals[second], page)}."
    )
    return _done(page, summary, names, before)


def _apply_place_visual(document: PbirDocument, op: PlaceVisual, model: ModelInfo) -> HandlerResult:
    page, names = _selection(document, [op.visual, op.relative_to], "Place")
    if len(names) != 2:
        raise DocumentError("A visual cannot be placed relative to itself")
    name, anchor_name = names
    box, anchor = page_box(page, name), page_box(page, anchor_name)
    x: float | None = None
    y: float | None = None
    if op.side == "right_of":
        x, y = anchor.right + op.gap, anchor.y
    elif op.side == "left_of":
        x, y = anchor.x - op.gap - box.width, anchor.y
    elif op.side == "below":
        x, y = anchor.x, anchor.bottom + op.gap
    elif op.side == "above":
        x, y = anchor.x, anchor.y - op.gap - box.height
    else:
        edge = op.edge or _apart_edge(box, anchor)
        x, y = _aligned(edge, box, anchor)
    page_width, page_height = _page_bounds(page)
    fitted_x = None if x is None else clamp(x, 0, page_width - box.width)
    fitted_y = None if y is None else clamp(y, 0, page_height - box.height)
    cramped = (x is not None and fitted_x != x) or (y is not None and fitted_y != y)
    before = leaf_boxes(page)
    move_box(document, page, name, x=fitted_x, y=fitted_y)
    moved_label = describe(page.visuals[name], page)
    anchor_label = describe(page.visuals[anchor_name], page)
    phrase = {
        "left_of": "to the left of",
        "right_of": "to the right of",
        "above": "above",
        "below": "below",
        "align_with": "in line with",
    }[op.side]
    result = _done(page, f"Placed {moved_label} {phrase} {anchor_label}.", [name], before)
    if cramped:
        result.warnings.insert(
            0, "There is not enough room on the page there, so it sits against the page edge."
        )
    return result


def _apart_edge(box: Box, anchor: Box) -> Edge:
    """The edge to line up by: tops for visuals side by side, left edges for stacked ones."""
    across = max(anchor.x - box.right, box.x - anchor.right, 0.0)
    down = max(anchor.y - box.bottom, box.y - anchor.bottom, 0.0)
    return "top" if across >= down else "left"


def _aligned(edge: Edge, box: Box, anchor: Box) -> tuple[float | None, float | None]:
    if edge == "left":
        return anchor.x, None
    if edge == "right":
        return anchor.right - box.width, None
    if edge == "center_h":
        return anchor.x + anchor.width / 2 - box.width / 2, None
    if edge == "top":
        return None, anchor.y
    if edge == "bottom":
        return None, anchor.bottom - box.height
    return None, anchor.y + anchor.height / 2 - box.height / 2


# --- a text box and a shape ---------------------------------------------------------------------


def _hex(document: PbirDocument, color: ColorValue) -> str:
    """A text run's color is a plain hex in the file, so a theme color is resolved now."""
    if isinstance(color, ThemeColorRef):
        return expr.resolve_theme_color(document.theme(), color.theme, color.percent)
    return expr.hex_color(color)


def _paragraphs(document: PbirDocument, op: AddTextbox) -> list[dict[str, Any]]:
    """The text as Desktop writes a text box: one paragraph per line, each with text runs
    that carry their own ``textStyle`` (font size in pt, hex color, ``bold`` weight)."""
    if any(ord(c) < 32 and c not in "\n\r\t" for c in op.text):
        raise DocumentError("The text cannot contain control characters")
    style: dict[str, Any] = {}
    if op.font_family:
        style["fontFamily"] = op.font_family
    if op.font_size is not None:
        style["fontSize"] = f"{op.font_size:g}pt"
    if op.color is not None:
        style["color"] = _hex(document, op.color)
    if op.bold:
        style["fontWeight"] = "bold"
    paragraphs: list[dict[str, Any]] = []
    for line in op.text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        run: dict[str, Any] = {"value": line}
        if style:
            run["textStyle"] = dict(style)
        paragraphs.append({"textRuns": [run]})
    return paragraphs


def _apply_add_textbox(document: PbirDocument, op: AddTextbox, model: ModelInfo) -> HandlerResult:
    page = resolve_page(document, op.page)
    width, height = default_size("textbox")
    placement = choose_placement(
        page,
        op.width or width,
        op.height or height,
        op.x,
        op.y,
        min_scale=1.0 if op.width or op.height else 0.5,
    )
    visual = new_visual(document, page, "textbox", placement)
    visual["visual"]["objects"] = {
        "general": [{"properties": {"paragraphs": _paragraphs(document, op)}}]
    }
    before = leaf_boxes(page)
    attach(document, page, visual)
    warnings = advisories(page, [str(visual["name"])], before=before, overlap=False)
    if placement.crowded:
        warnings.insert(0, CROWDED)
    first_line = " ".join(op.text.split())[:60]
    return HandlerResult(
        f"Added a text box saying '{first_line}' to page '{page.display_name}'.",
        warnings=warnings,
    )


def _apply_add_shape(document: PbirDocument, op: AddShape, model: ModelInfo) -> HandlerResult:
    page = resolve_page(document, op.page)
    width, height = default_size("shape")
    if op.shape == "line":
        width, height = 200.0, 10.0
    placement = choose_placement(
        page,
        op.width or width,
        op.height or height,
        op.x,
        op.y,
        min_scale=1.0 if op.width or op.height else 0.5,
    )
    visual = new_visual(document, page, "shape", placement)
    default = expr.state_selector(expr.STATE_DEFAULT)
    _set(document, visual, "shape", "tileShape", op.shape, default)
    if op.fill is not None:
        _set(document, visual, "fill", "show", True, default)
        _set(document, visual, "fill", "fillColor", _color_arg(op.fill), default)
    if op.outline is not None:
        _set(document, visual, "outline", "show", True, default)
        _set(document, visual, "outline", "lineColor", _color_arg(op.outline), default)
    before = leaf_boxes(page)
    attach(document, page, visual)
    warnings = advisories(page, [str(visual["name"])], before=before, overlap=False)
    if placement.crowded:
        warnings.insert(0, CROWDED)
    word = re.sub(
        r"(?<!^)(?=[A-Z])", " ", op.shape
    ).lower()  # "rectangleRounded" -> "rectangle rounded"
    notes: list[VisibleIn] = []
    if op.shape not in DRAWN_SHAPES:
        notes.append(
            VisibleIn(
                "add_shape",
                "powerbi_only",
                f"Studio draws the {word} shape as a plain rectangle; Power BI draws it properly.",
            )
        )
    article = "an" if word[0] in "aeiou" else "a"
    return HandlerResult(
        f"Added {article} {word} shape to page '{page.display_name}'.",
        warnings=warnings,
        visible_in=notes,
    )


ALIGN_OPERATIONS = operation_classes(AlignOperation)
ALIGN_HANDLERS: dict[str, Handler] = {
    "align_visuals": _apply_align_visuals,
    "distribute_visuals": _apply_distribute_visuals,
    "swap_visuals": _apply_swap_visuals,
    "place_visual": _apply_place_visual,
    "add_textbox": _apply_add_textbox,
    "add_shape": _apply_add_shape,
}
