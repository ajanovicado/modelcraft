"""Where visuals sit on a page, in page pixels, and how a position is written back.

A member of a visual group stores ``position.x``/``y`` relative to its group's origin, and a
group inside a group adds up (measured in Power BI Desktop 2.157: a child stored at 100,100 in
a group at 300,300 drew at 400,400). Everything a user says about a position ("move it to
x=100", "align these", "put A below B") is in page pixels, so the layout operations read boxes
through this module and write them through ``write_box``, which subtracts the group origin
again. Bounds and overlap checks use page pixels too.

The module also names visuals the way a user reads them (by caption, never by id) and turns
"there is no such visual" into a list of the ones that exist.

Pure: it reads and edits the ``PageDoc``/visual dicts it is given and never touches the
package. Imported by ``report_edit_layout`` and ``report_edit_align``.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_captions import DYNAMIC_TITLE, visible_caption
from .report_edit_context import _page_bounds, _projections
from .report_roles import Box, free_position, overlaps, projection_label, visual_type_name

# Sub-pixel noise in a stored position (Desktop writes 17 digits) is not a move.
TOLERANCE = 0.5
NOISE = 0.01  # below this a coordinate did not change
CROWDED = "There is no free space left on the page, so it was put where it covers the least."
# A backdrop is a shape or picture that sits behind other visuals on purpose (a panel, a
# header band): it must not push new visuals off the page or count as a collision.
_BACKDROP_TYPES = frozenset({"shape", "image"})
_MAX_CAPTION = 60
_MAX_NOTES = 4


# --- numbers --------------------------------------------------------------------------


def number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return default
    return float(value)


def tidy(value: float) -> int | float:
    """An integral position is written as an integer, the way Desktop writes 100 (not 100.0)."""
    return int(value) if float(value).is_integer() else float(value)


def show(value: float) -> str:
    """A pixel value for a sentence: 100, or 96.6 when it is not whole."""
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, max(low, high)))


# --- the visual tree of a page ---------------------------------------------------------


def is_group(visual: Mapping[str, Any]) -> bool:
    return isinstance(visual.get("visualGroup"), dict)


def visual_kind(visual: Mapping[str, Any]) -> str:
    inner = visual.get("visual")
    return str(inner.get("visualType", "")) if isinstance(inner, dict) else ""


def ancestors(page: PageDoc, name: str) -> list[str]:
    """The groups a visual sits in, nearest first (cycle-safe)."""
    found: list[str] = []
    seen = {name}
    parent = page.visuals.get(name, {}).get("parentGroupName")
    while isinstance(parent, str) and parent in page.visuals and parent not in seen:
        found.append(parent)
        seen.add(parent)
        parent = page.visuals[parent].get("parentGroupName")
    return found


def descendants(page: PageDoc, name: str) -> list[str]:
    """Every visual inside a group, nested groups included, breadth first."""
    found: list[str] = []
    queue = [name]
    while queue:
        current = queue.pop(0)
        for child, visual in page.visuals.items():
            if visual.get("parentGroupName") == current and child != name and child not in found:
                found.append(child)
                queue.append(child)
    return found


def siblings(page: PageDoc, name: str) -> list[str]:
    """The visuals that share this one's parent (the top level when it has none)."""
    parent = page.visuals[name].get("parentGroupName")
    parent = parent if isinstance(parent, str) and parent in page.visuals else None
    return [
        other
        for other, visual in page.visuals.items()
        if (
            visual.get("parentGroupName") if visual.get("parentGroupName") in page.visuals else None
        )
        == parent
    ]


def origin(page: PageDoc, name: str) -> tuple[float, float]:
    """Where the visual's stored coordinates start on the page: the sum of its groups' own
    stored positions (0, 0 for a visual on the page itself)."""
    x = y = 0.0
    for group in ancestors(page, name):
        position = page.visuals[group].get("position")
        if isinstance(position, dict):
            x += number(position.get("x"))
            y += number(position.get("y"))
    return x, y


def page_box(page: PageDoc, name: str) -> Box:
    """The visual's rectangle in page pixels."""
    position = page.visuals[name].get("position")
    stored: Mapping[str, Any] = position if isinstance(position, dict) else {}
    ox, oy = origin(page, name)
    return Box(
        number(stored.get("x")) + ox,
        number(stored.get("y")) + oy,
        number(stored.get("width")),
        number(stored.get("height")),
    )


def leaf_boxes(page: PageDoc) -> dict[str, Box]:
    """Every visual that draws something (groups only frame others), in page pixels. Hidden
    visuals are included: they still take room when someone shows them."""
    return {name: page_box(page, name) for name, v in page.visuals.items() if not is_group(v)}


def _contains(outer: Box, inner: Box) -> bool:
    return (
        inner.x >= outer.x - 1
        and inner.y >= outer.y - 1
        and inner.right <= outer.right + 1
        and inner.bottom <= outer.bottom + 1
    )


def backdrops(page: PageDoc, boxes: Mapping[str, Box]) -> set[str]:
    """Shapes and pictures that have other visuals inside them: panels drawn on purpose."""
    found: set[str] = set()
    for name, box in boxes.items():
        if visual_kind(page.visuals[name]) not in _BACKDROP_TYPES:
            continue
        if any(other != name and _contains(box, other_box) for other, other_box in boxes.items()):
            found.add(name)
    return found


def occupied(page: PageDoc, exclude: Collection[str] = ()) -> dict[str, Box]:
    """The boxes a new or moved visual should keep clear of."""
    boxes = leaf_boxes(page)
    skipped = backdrops(page, boxes) | set(exclude)
    return {name: box for name, box in boxes.items() if name not in skipped}


def write_box(
    page: PageDoc,
    name: str,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
) -> None:
    """Set the visual's position from page pixels; only the values given are written, and x/y
    lose the group origin so the stored value stays relative to the group."""
    visual = page.visuals[name]
    position = visual.setdefault("position", {})
    ox, oy = origin(page, name)
    if x is not None:
        position["x"] = tidy(x - ox)
    if y is not None:
        position["y"] = tidy(y - oy)
    if width is not None:
        position["width"] = tidy(width)
    if height is not None:
        position["height"] = tidy(height)


def move_box(
    document: PbirDocument,
    page: PageDoc,
    name: str,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
) -> bool:
    """``write_box`` for the values that differ from the visual's box by more than float
    noise, and mark the visual changed. Returns whether anything was written."""
    box = page_box(page, name)
    changes: dict[str, float] = {}
    for key, wanted, current in (
        ("x", x, box.x),
        ("y", y, box.y),
        ("width", width, box.width),
        ("height", height, box.height),
    ):
        if wanted is not None and abs(wanted - current) > NOISE:
            changes[key] = wanted
    if changes:
        write_box(page, name, **changes)
        document.touch_visual(page, name)
    return bool(changes)


def next_tab_order(page: PageDoc) -> int:
    """Desktop's next tab stop: the highest one on the page plus 1000 (0 on an empty page)."""
    found = [
        number(v["position"].get("tabOrder"), -1.0)
        for v in page.visuals.values()
        if isinstance(v.get("position"), dict) and "tabOrder" in v["position"]
    ]
    return int(max(found) + 1000) if found else 0


def next_z(page: PageDoc) -> int:
    found = [
        number(v["position"].get("z"))
        for v in page.visuals.values()
        if isinstance(v.get("position"), dict) and "z" in v["position"]
    ]
    return int(max(found) + 1000) if found else 0


# --- naming visuals the way a user reads them ---------------------------------------------


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _MAX_CAPTION else text[: _MAX_CAPTION - 1].rstrip() + "…"


def type_word(visual: Mapping[str, Any]) -> str:
    """ "clustered column chart", "slicer", "action button": what Power BI calls the type."""
    if is_group(visual):
        return "group"
    kind = visual_kind(visual) or "visual"
    known = visual_type_name(kind)
    if known != kind:
        return known
    return re.sub(r"(?<!^)(?=[A-Z])", " ", kind).lower()


def _caption(visual: dict[str, Any]) -> str | None:
    group = visual.get("visualGroup")
    if isinstance(group, dict):
        name = str(group.get("displayName") or "").strip()
        return name or None
    caption = visible_caption(visual)
    return None if caption is None or caption == DYNAMIC_TITLE else caption


def describe(visual: dict[str, Any], page: PageDoc | None = None) -> str:
    """The visual as a person would point at it: the 'Sales by channel' clustered column
    chart, or the table of Agent ID when it has no caption. Never an internal id."""
    word = type_word(visual)
    caption = _caption(visual)
    if is_group(visual):
        return f"the group '{_short(caption)}'" if caption else "a group of visuals"
    if caption:
        return f"the '{_short(caption)}' {word}"
    first = next(iter(_projections(visual)), None)
    if first is not None:
        return f"the {word} of {_short(projection_label(first))}"
    name = visual.get("name")
    if page is not None and isinstance(name, str) and name in page.visuals:
        box = page_box(page, name)
        return f"the {word} at {show(box.x)}, {show(box.y)}"
    return f"the {word}"


def sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def join_names(names: Sequence[str], limit: int = 3) -> str:
    unique = list(dict.fromkeys(names))
    shown = unique[:limit]
    extra = len(unique) - len(shown)
    if extra:
        return f"{', '.join(shown)} and {extra} more"
    if len(shown) > 1:
        return f"{', '.join(shown[:-1])} and {shown[-1]}"
    return "".join(shown)


def visual_choices(document: PbirDocument, limit: int = 6) -> str:
    """ "Visuals in this report: the 'A' card, the 'B' table and 4 more." for a missing target."""
    names = [
        describe(visual, page) for page, _, visual in document.all_visuals() if not is_group(visual)
    ]
    return f"Visuals in this report: {join_names(names, limit)}." if names else ""


def resolve_visual(document: PbirDocument, ref: str) -> tuple[PageDoc, dict[str, Any]]:
    """A visual (or group) by its id, else by its caption when exactly one has it. A miss
    lists the visuals there are; nothing names an internal id."""
    try:
        return document.visual(ref)
    except DocumentError:
        pass
    wanted = " ".join(ref.split()).casefold()
    found = [
        (page, visual)
        for page, _, visual in document.all_visuals()
        if wanted and " ".join((_caption(visual) or "").split()).casefold() == wanted
    ]
    if len(found) == 1:
        return found[0]
    if found:
        # The page tells apart two visuals with the same caption.
        named = [f"{describe(v, p)} on page '{p.display_name}'" for p, v in found]
        if len(set(named)) < len(named):  # same caption on one page: tell them apart by place
            boxes = [page_box(p, str(v["name"])) for p, v in found]
            named = [
                f"{text} at {show(box.x)}, {show(box.y)}"
                for text, box in zip(named, boxes, strict=True)
            ]
        options = join_names(named, len(found))
        raise DocumentError(f"More than one visual is called {ref!r}: {options}. Say which one.")
    choices = visual_choices(document)
    raise DocumentError(f"There is no visual {ref!r} in this report. {choices}".rstrip())


def resolve_leaf(document: PbirDocument, ref: str) -> tuple[PageDoc, dict[str, Any]]:
    """A visual that has fields and formatting of its own (not a group)."""
    page, visual = resolve_visual(document, ref)
    if is_group(visual):
        raise DocumentError(
            f"{sentence(describe(visual, page))} holds other visuals; edit the visuals inside it"
        )
    return page, visual


def resolve_page(document: PbirDocument, ref: str) -> PageDoc:
    try:
        return document.page(ref)
    except DocumentError:
        pages = join_names([f"'{p.display_name}'" for p in document.pages.values()], 8)
        raise DocumentError(f"There is no page named {ref!r}. Pages: {pages}.") from None


# --- where a new visual goes -------------------------------------------------------------


@dataclass(frozen=True)
class Placement:
    x: float
    y: float
    width: float
    height: float
    # A spot was searched for (no x/y given) and none is free: the least covered one is used.
    crowded: bool = False
    explicit: bool = False


def choose_placement(
    page: PageDoc,
    width: float,
    height: float,
    x: float | None = None,
    y: float | None = None,
    *,
    prefer: Box | None = None,
    exclude: Collection[str] = (),
    min_scale: float = 1.0,
) -> Placement:
    """Page pixels for a new visual: the given x/y clamped to the page, else the first free
    spot (hidden visuals count as occupied). The size is cut down to the page.

    With ``min_scale`` below 1 (a visual whose size nobody asked for) a crowded page is first
    tried with the same shape made smaller, down to that fraction, so a new chart lands in a
    real gap instead of on top of a card (the Agents dashboard has no free 400 x 300 spot but
    room for a 300 x 150 chart above its cards)."""
    page_width, page_height = _page_bounds(page)
    width, height = min(width, page_width), min(height, page_height)
    taken = list(occupied(page, exclude).values())
    if x is not None and y is not None:
        return Placement(
            clamp(x, 0, page_width - width),
            clamp(y, 0, page_height - height),
            width,
            height,
            explicit=True,
        )
    found_x, found_y = free_position(page_width, page_height, taken, width, height, prefer=prefer)
    crowded = bool(overlaps(Box(found_x, found_y, width, height), taken))
    scale = 1.0
    while crowded and x is None and y is None and scale - 0.05 >= min_scale - 1e-9:
        scale = round(scale - 0.05, 2)
        smaller_width, smaller_height = width * scale, height * scale
        spot_x, spot_y = free_position(
            page_width, page_height, taken, smaller_width, smaller_height, prefer=prefer
        )
        if not overlaps(Box(spot_x, spot_y, smaller_width, smaller_height), taken):
            found_x, found_y, width, height, crowded = (
                spot_x,
                spot_y,
                smaller_width,
                smaller_height,
                False,
            )
    return Placement(
        clamp(found_x if x is None else x, 0, page_width - width),
        clamp(found_y if y is None else y, 0, page_height - height),
        width,
        height,
        crowded=crowded,
        explicit=x is not None or y is not None,
    )


def new_visual(
    document: PbirDocument, page: PageDoc, visual_type: str, placement: Placement
) -> dict[str, Any]:
    """The bare visual.json Desktop starts every visual from: a name, the box, a z above every
    other and the next tab stop (tabOrder is not derived from z)."""
    name = document.new_name(document.visual_names())
    return {
        "$schema": document.schema("visualContainer"),
        "name": name,
        "position": {
            "x": tidy(placement.x),
            "y": tidy(placement.y),
            "z": next_z(page),
            "height": tidy(placement.height),
            "width": tidy(placement.width),
            "tabOrder": next_tab_order(page),
        },
        "visual": {"visualType": visual_type, "drillFilterOtherVisuals": True},
    }


def attach(document: PbirDocument, page: PageDoc, visual: dict[str, Any]) -> None:
    name = str(visual["name"])
    page.visuals[name] = visual
    document.touch_visual(page, name)


# --- advisories ---------------------------------------------------------------------------


def _edges(box: Box, page_width: float, page_height: float) -> list[str]:
    found = []
    if box.x < -TOLERANCE:
        found.append("left edge")
    if box.y < -TOLERANCE:
        found.append("top edge")
    if box.right > page_width + TOLERANCE:
        found.append("right edge")
    if box.bottom > page_height + TOLERANCE:
        found.append("bottom edge")
    return found


def advisories(
    page: PageDoc,
    names: Collection[str],
    *,
    before: Mapping[str, Box] | None = None,
    overlap: bool = True,
    together: Collection[str] = (),
) -> list[str]:
    """Advisory sentences (never blocking) about visuals whose box was just set.

    A visual past the page's edge, one that now covers another (only pairs that did not
    already overlap when ``before`` says so; panels drawn behind others are ignored), and a
    member that no longer fits its group's frame. A group counts through its members. Shapes,
    pictures and text boxes are overlays by nature and are not checked for overlap.
    """
    boxes = leaf_boxes(page)
    page_width, page_height = _page_bounds(page)
    targets: list[str] = []
    for name in names:
        visual = page.visuals.get(name)
        if visual is None:
            continue
        members = descendants(page, name) if is_group(visual) else [name]
        targets.extend(m for m in members if m in boxes and m not in targets)
    others = occupied(page)
    notes: list[str] = []
    for name in targets:
        visual, box = page.visuals[name], boxes[name]
        label = sentence(describe(visual, page))
        edges = _edges(box, page_width, page_height)
        if edges:
            notes.append(f"{label} sticks out past the {join_names(edges)} of the page.")
        parent = next(iter(ancestors(page, name)), None)
        if parent is not None and name in names and not _contains(page_box(page, parent), box):
            notes.append(
                f"{label} is now outside the frame of {describe(page.visuals[parent], page)}."
            )
        if not overlap or visual_kind(visual) in _BACKDROP_TYPES | {"textbox"}:
            continue
        # Copies made together (a duplicated group) overlap each other as their sources did.
        covered = [
            n
            for n, b in others.items()
            if n != name and not (n in together and name in together) and overlaps(box, [b])
        ]
        moved = before is not None and name in before
        if before is not None and moved:
            covered = [n for n in covered if not overlaps(before[name], [before.get(n, others[n])])]
        if covered:
            verb = "now overlaps" if moved else "overlaps"
            names_text = join_names([describe(page.visuals[n], page) for n in covered])
            notes.append(f"{label} {verb} {names_text}.")
    if len(notes) > _MAX_NOTES:
        notes = [*notes[: _MAX_NOTES - 1], f"{len(notes) - _MAX_NOTES + 1} more layout notes."]
    return notes
