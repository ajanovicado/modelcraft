"""Layout requests read into the layout operations: line visuals up, space them evenly, swap two,
put one beside another, centre one on the page.

``align_visuals``, ``distribute_visuals``, ``swap_visuals`` and ``place_visual`` take page pixels
and work out group positions and page limits themselves, so nothing here computes a coordinate:
the parser names the visuals and the edge, the operation moves them. (Before those operations
existed each request became one ``set_position`` per visual with numbers worked out here.)
"""

from __future__ import annotations

import re
from typing import Any

from .edit_context import VisualEntry
from .edit_frame import Frame, names_phrase, need_entries
from .edit_targets import find_page, find_types, resolve_visuals
from .edit_text import PROPERTY_NOUNS
from .edit_types import Choice, Clause

Op = dict[str, Any]

MAX_VISUALS = 50  # what one align or distribute operation takes

# --- swap ----------------------------------------------------------------------------------------

_SWAP = re.compile(
    r"\bswap\b|\bexchange\b|\bswitch\s+(?:the\s+)?(?:places|positions)\b|\btrade\s+places\b", re.I
)


def _swap(f: Frame, a: VisualEntry, b: VisualEntry) -> Clause:
    if a.id == b.id:
        return f.clarify("Name two different visuals to swap.")
    if a.page_id != b.page_id:
        return f.not_available(
            "Studio swaps visuals on the same page; these are on different pages."
        )
    made: list[Op] = [{"op": "swap_visuals", "a": a.id, "b": b.id}]
    return f.apply([a, b], made, f"Swapped the places of {a.label} and {b.label}.", how="position")


def swap_clause(f: Frame) -> Clause | None:
    """ "swap the top left chart and the top right chart", "swap the two top charts"."""
    if not _SWAP.search(f.masked):
        return None
    if re.search(r"\b(?:field|fields|axes|axis|rows|columns)\b", f.masked) and not find_types(
        f.masked
    ):
        return None
    parts = re.split(r"\s+(?:and|with|&|for|places\s+with)\s+", f.text, maxsplit=1, flags=re.I)
    if len(parts) < 2:
        # "swap the two top charts": one phrase that names exactly two visuals
        whole = resolve_visuals(f.text, f.context, carry=None, forced=f.forced.visuals)
        if whole.ok and len(whole.entries) == 2:
            return _swap(f, whole.entries[0], whole.entries[1])
        return f.clarify("Which two visuals should swap places?")
    # "swap the pie chart and the line chart on Product Mix": the page named once is the page
    # of both
    page, _ = find_page(f.text, f.context)
    where = f" on page {page.index + 1}" if page is not None else ""
    first = resolve_visuals(parts[0] + where, f.context, carry=None, forced=None)
    second = resolve_visuals(parts[1] + where, f.context, carry=None, forced=None)
    for res in (first, second):
        if not res.ok or len(res.entries) != 1:
            f.res = res
            return (
                f.ask_target()
                if res.candidates or res.question
                else f.clarify("Which two visuals should swap places?")
            )
    return _swap(f, first.entries[0], second.entries[0])


# --- place one visual by another ---------------------------------------------------------------

_MOVE_WORD = re.compile(r"\b(?:move|put|place|position|shift|drag|slide|push|set)\b", re.I)
_RELATION = re.compile(
    r"\b(?P<rel>to\s+the\s+(?:left|right)\s+of|(?:left|right)\s+of|next\s+to|beside|alongside|"
    r"below|under(?:neath)?|beneath|above|over|after|before|on\s+the\s+(?:left|right)\s+of)\b",
    re.I,
)
_SIDES = {
    "below": "below",
    "under": "below",
    "underneath": "below",
    "beneath": "below",
    "above": "above",
    "over": "above",
    "left of": "left_of",
    "to the left of": "left_of",
    "on the left of": "left_of",
    "before": "left_of",
}
_SIDE_WORDS = {
    "below": "below",
    "above": "above",
    "left_of": "to the left of",
    "right_of": "next to",
}
_GAP = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pixels?)\s*(?:gap|apart|away|of\s+(?:space|spacing)|spacing|margin|"
    r"(?:to\s+the\s+)?(?:left|right)\s+of|above|below|under\w*|beneath|beside|next\s+to)"
    r"|\b(?:gap|spacing|margin|space)\s*(?:of|=|:)?\s*(\d+(?:\.\d+)?)\s*(?:px|pixels?)\b",
    re.I,
)


def _gap(text: str) -> float | None:
    """The distance a request asks to keep between two visuals ("20 px to the right of")."""
    found = _GAP.search(text)
    if not found:
        return None
    value = float(found.group(1) or found.group(2))
    return value if 0 <= value <= 2000 else None


def _side_ops(
    entries: list[VisualEntry], anchor: VisualEntry, side: str, gap: float | None
) -> list[Op]:
    """One ``place_visual`` per visual. Several visuals go beside each other, not on top of
    each other: after the first, each one is placed against the one before it, along the
    other axis (a row under an anchor, a column beside it)."""
    made: list[Op] = []
    previous: VisualEntry | None = None
    along = "right_of" if side in {"below", "above"} else "below"
    for entry in entries:
        op: Op = {
            "op": "place_visual",
            "visual": entry.id,
            "relative_to": (previous or anchor).id,
            "side": side if previous is None else along,
        }
        if gap is not None:
            op["gap"] = gap
        made.append(op)
        previous = entry
    return made


def place_clause(f: Frame) -> Clause | None:
    """One visual placed by another: "put the table below the cards", "slicer left of chart"."""
    if not _MOVE_WORD.search(f.masked) or PROPERTY_NOUNS.search(f.masked):
        return None
    match = _RELATION.search(f.text)
    if match is None:
        return None
    subject_text, reference_text = f.text[: match.start()], f.text[match.end() :]
    if not find_types(reference_text) and not re.search(r"\w{3,}", reference_text):
        return None
    relation = re.sub(r"\s+", " ", match.group("rel").casefold())
    side = _SIDES.get(relation, "right_of")
    reference = resolve_visuals(reference_text, f.context, carry=None, forced=None)
    if not reference.ok:
        if not reference.candidates and not reference.question:
            return None
        f.res = reference
        return f.ask_target()
    subject = resolve_visuals(subject_text, f.context, carry=f.carry, forced=f.forced.visuals)
    f.res = subject
    entries = need_entries(f)
    if isinstance(entries, Clause):
        return entries
    anchors = reference.entries
    entries = [e for e in entries if e.id not in {r.id for r in anchors}]
    if not entries:
        return f.clarify("Name a different visual to move.")
    gap = _gap(f.text)
    words = _SIDE_WORDS[side]
    # "below the cards" is below the lowest of them: the one that sets the edge is the anchor
    anchor = _extreme(anchors, side)
    made = _side_ops(entries, anchor, side, gap)
    return f.apply(
        entries,
        made,
        f"Moved {names_phrase(entries)} {words} {names_phrase(anchors)}.",
        how="position",
    )


def _extreme(anchors: list[VisualEntry], side: str) -> VisualEntry:
    """The visual of a group that a request "below", "above", "left of" or "next to" the whole
    group is measured from."""
    if side == "below":
        return max(anchors, key=lambda e: e.y + e.height)
    if side == "above":
        return min(anchors, key=lambda e: e.y)
    if side == "left_of":
        return min(anchors, key=lambda e: e.x)
    return max(anchors, key=lambda e: e.x + e.width)


# --- align ---------------------------------------------------------------------------------------

_ALIGN_VERB = re.compile(r"\balign(?:ed)?\b|\bline\s+(?:them\s+|it\s+)?up\b|\blined\s+up\b", re.I)
_EDGE = re.compile(r"\b(left|right|top|bottom|center|centre|middle)s?\b")
_TO_PAGE = re.compile(
    r"\b(?:to|against|along|within)\s+(?:the\s+)?(?:page|canvas)\b|"
    r"\b(?:top|bottom|left|right|middle|center|centre|edge|side|margin)s?\s+of\s+the\s+"
    r"(?:page|canvas)\b|\b(?:page|canvas)\s+(?:edge|margin|border|side)\b",
    re.I,
)
_EDGE_PHRASE = {
    "left": "left",
    "right": "right",
    "top": "top",
    "bottom": "bottom",
    "center_h": "center",
    "center_v": "middle",
}


def _in_a_row(entries: list[VisualEntry]) -> bool:
    ordered = sorted(entries, key=lambda e: e.cy)
    return all(abs(e.cy - ordered[0].cy) <= 0.5 * min(e.height, ordered[0].height) for e in ordered)


def _in_a_column(entries: list[VisualEntry]) -> bool:
    ordered = sorted(entries, key=lambda e: e.cx)
    return all(abs(e.cx - ordered[0].cx) <= 0.5 * min(e.width, ordered[0].width) for e in ordered)


MANY_VISUALS = 8  # lining up this many visuals at once asks first
_EVERYTHING = re.compile(r"\b(?:all|everything|every\s+visual|each\s+visual)\b", re.I)


def _edge_of(word: str | None, low: str, entries: list[VisualEntry]) -> str:
    """The edge a request names. Without one, visuals in a row line up by their tops and
    others by their left edges; "center" is the middle line the visuals share: the vertical
    middle of a row, the horizontal middle of a column."""
    if word in {"center", "centre", "middle"}:
        if re.search(r"\bvertical(?:ly)?\b", low):
            return "center_v"
        if re.search(r"\bhorizontal(?:ly)?\b", low):
            return "center_h"
        return "center_v" if len(entries) > 1 and _in_a_row(entries) else "center_h"
    if word in {"left", "right", "top", "bottom"}:
        return word
    return "top" if len(entries) > 1 and _in_a_row(entries) else "left"


def align_clause(f: Frame) -> Clause | None:
    """ "align the slicers to the right", "line up the cards", "align the table with the chart",
    "align the cards to the top of the page"."""
    if not _ALIGN_VERB.search(f.masked):
        return None
    if re.search(r"\b(?:title|heading|caption|text|legend|labels?)\b", f.masked) and not re.search(
        r"\b(?:visuals?|charts?|cards?|slicers?|tables?)\b", f.masked
    ):
        return None  # title alignment is a format
    edge_match = _EDGE.search(f.masked)
    word = edge_match.group(1) if edge_match else None
    reference = re.search(r"\b(?:with|to|and|against)\s+(.+)$", f.text, re.I)
    references: list[VisualEntry] = []
    subject_text = f.text
    if reference and find_types(reference.group(1)):
        candidate = resolve_visuals(reference.group(1), f.context, carry=None, forced=None)
        if candidate.ok and len(candidate.entries) == 1:
            references = candidate.entries
        elif 1 < len(candidate.candidates) <= 6:
            references = candidate.candidates
        if references:
            subject_text = f.text[: reference.start()]
    f.res = resolve_visuals(subject_text, f.context, carry=f.carry, forced=f.forced.visuals)
    entries = need_entries(f)
    if isinstance(entries, Clause):
        return entries
    to_page = not references and bool(_TO_PAGE.search(f.low))
    if references:
        group = [e for e in entries if e.id not in {r.id for r in references}]
        if not group:
            return f.clarify("Align it with what? Name another visual, or select several.")
        edge = _edge_of(word, f.low, [references[0], *group])

        def aligned(ref: VisualEntry) -> list[Op]:
            return [
                {
                    "op": "place_visual",
                    "visual": e.id,
                    "relative_to": ref.id,
                    "side": "align_with",
                    "edge": edge,
                }
                for e in group
            ]

        if len(references) > 1:
            # which of several charts it is lined up with is not for the parser to guess
            return f.clarify(
                f"Align {names_phrase(group)} with which one? "
                + ", ".join(r.label for r in references[:5]),
                [
                    Choice(
                        label=r.label,
                        ops=tuple(aligned(r)),
                        summary=f"Aligned {names_phrase(group)} with {r.label} "
                        f"({_EDGE_PHRASE[edge]}).",
                    )
                    for r in references
                ],
                group,
            )
        summary = (
            f"Aligned {names_phrase(group)} with {references[0].label} ({_EDGE_PHRASE[edge]})."
        )
        return f.apply(group, aligned(references[0]), summary, how="position")
    if len(entries) < 2 and not to_page:
        return f.clarify("Align it with what? Name another visual, or select several.")
    if len(entries) > MAX_VISUALS:
        return f.clarify(f"I can line up at most {MAX_VISUALS} visuals at a time.")
    ids = [e.id for e in entries]
    target = "page" if to_page else "selection"
    where = " of the page" if to_page else ""

    def line_up(edge: str) -> list[Op]:
        return [{"op": "align_visuals", "visuals": ids, "edge": edge, "to": target}]

    # No edge said: a row lines up by its tops and a column by its left edges. Visuals that are
    # neither would land on top of each other on a guessed edge, so the person picks.
    if (
        word is None
        and not to_page
        and len(entries) > 1
        and not (_in_a_row(entries) or _in_a_column(entries))
    ):
        return f.clarify(
            f"Line up {names_phrase(entries)} by which edge?",
            [
                Choice(
                    label=label,
                    ops=tuple(line_up(edge)),
                    summary=f"Aligned {names_phrase(entries)} to the {_EDGE_PHRASE[edge]}.",
                )
                for label, edge in (
                    ("Left edges", "left"),
                    ("Tops", "top"),
                    ("Centers", "center_h"),
                )
            ],
            entries,
        )
    edge = _edge_of(word, f.low, entries)
    summary = f"Aligned {names_phrase(entries)} to the {_EDGE_PHRASE[edge]}{where}."
    if len(entries) >= MANY_VISUALS or _EVERYTHING.search(f.low):
        # every visual of a page moved onto one line is rarely what was meant: ask first
        return f.clarify(
            f"That lines up {len(entries)} visuals on one edge, which can stack them on top of "
            "each other. Go ahead?",
            [
                Choice(
                    label=f"Yes, align {len(entries)} visuals",
                    ops=tuple(line_up(edge)),
                    summary=summary,
                )
            ],
            entries,
        )
    return f.apply(entries, line_up(edge), summary, how="position")


# --- centre on the page --------------------------------------------------------------------------

_CENTER = re.compile(r"\bcent(?:er|re)(?:e?d)?\b|\bin\s+the\s+middle\b", re.I)


def center_clause(f: Frame) -> Clause | None:
    """Center a visual: horizontally on the page; "center it vertically" or "in the middle of
    the page" also sets the vertical position."""
    if not _CENTER.search(f.masked) or PROPERTY_NOUNS.search(f.masked):
        return None
    entries = need_entries(f)
    if isinstance(entries, Clause):
        return entries
    vertical = bool(re.search(r"\bvertical(?:ly)?\b", f.masked))
    horizontal = bool(re.search(r"\bhorizontal(?:ly)?\b", f.masked))
    # ("in the middle" is a position word the target resolver has already blanked in `masked`)
    both = bool(re.search(r"\b(?:both|exactly)\b|\bin\s+the\s+middle\b", f.low)) or (
        vertical and horizontal
    )
    ids = [e.id for e in entries]
    made: list[Op] = []
    # (with neither word said, horizontally is what centering a visual means)
    if both or not vertical:
        made.append({"op": "align_visuals", "visuals": ids, "edge": "center_h", "to": "page"})
    if both or vertical:
        made.append({"op": "align_visuals", "visuals": ids, "edge": "center_v", "to": "page"})
    how = "vertically and horizontally" if both else "vertically" if vertical else "horizontally"
    return f.apply(
        entries,
        made,
        f"Centered {names_phrase(entries)} {how} on the page.",
        how="position",
    )


# --- space evenly, in a row or a column ---------------------------------------------------------

_DISTRIBUTE = re.compile(
    r"\bdistribut\w*\b|\b(?:space|spread)\b.{0,40}\bevenly\b|\bevenly\s+(?:space|spread|distribut)\w*|"
    r"\bequal(?:ly)?\s+(?:spac|distribut)\w*|\b(?:same|equal|even)\s+(?:gaps?|spacing|distance)\b|"
    r"\bspace\s+(?:them|these|those|the\s+\w+)\s+out\b",
    re.I,
)
_HORIZONTAL = re.compile(
    r"\bhorizontal(?:ly)?\b|\bacross\b|\bleft\s+to\s+right\b|\bside\s+by\s+side\b|"
    r"\bin\s+a\s+row\b|\bsideways\b",
    re.I,
)
_VERTICAL = re.compile(
    r"\bvertical(?:ly)?\b|\btop\s+to\s+bottom\b|\bin\s+a\s+column\b|\bup\s+and\s+down\b|"
    r"\bstacked\b|\bdown\s+the\s+page\b",
    re.I,
)


def _spread_axis(entries: list[VisualEntry], low: str) -> str:
    """ "horizontal" or "vertical": what the request says, else the direction the visuals are
    already spread the most in."""
    if _HORIZONTAL.search(low) and not _VERTICAL.search(low):
        return "horizontal"
    if _VERTICAL.search(low) and not _HORIZONTAL.search(low):
        return "vertical"
    across = max(e.cx for e in entries) - min(e.cx for e in entries)
    down = max(e.cy for e in entries) - min(e.cy for e in entries)
    return "horizontal" if across >= down else "vertical"


def distribute_clause(f: Frame) -> Clause | None:
    """ "space the cards evenly", "distribute the slicers vertically"."""
    if not _DISTRIBUTE.search(f.masked) or PROPERTY_NOUNS.search(f.masked):
        return None
    f.res = resolve_visuals(f.text, f.context, carry=f.carry, forced=f.forced.visuals)
    entries = need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if len(entries) < 3:
        return f.not_available(
            "Spacing evenly needs at least three visuals: the two outermost stay where they are "
            "and the ones between are spaced out."
        )
    if len(entries) > MAX_VISUALS:
        return f.clarify(f"I can space at most {MAX_VISUALS} visuals at a time.")
    axis = _spread_axis(entries, f.low)
    made: list[Op] = [
        {"op": "distribute_visuals", "visuals": [e.id for e in entries], "axis": axis}
    ]
    way = "from left to right" if axis == "horizontal" else "from top to bottom"
    return f.apply(
        entries,
        made,
        f"Spaced {names_phrase(entries)} evenly {way}.",
        how="position",
    )


_ROW_VERB = re.compile(
    r"\b(?:put|place|arrange|line|lay|make|stack|organi[sz]e|set|get|move)\b", re.I
)
_ROW = re.compile(
    r"\b(?:in|into|as|on)\s+(?:a|one|the\s+same)\s+(?P<shape>row|column|line)\b|"
    r"\bside\s+by\s+side\b|\bone\s+(?:above|under|below)\s+the\s+other\b",
    re.I,
)


def row_clause(f: Frame) -> Clause | None:
    """ "put the cards in a row", "arrange the slicers in a column": lined up by their tops (or
    left edges), then spaced evenly when there are three or more."""
    if not _ROW_VERB.search(f.masked) or PROPERTY_NOUNS.search(f.masked):
        return None
    found = _ROW.search(f.masked)
    if found is None:
        return None
    shape = (found.group("shape") or "").casefold()
    text = found.group(0).casefold()
    column = shape == "column" or "above" in text or "under" in text or "below" in text
    f.res = resolve_visuals(
        f.text[: found.start()] or f.text, f.context, carry=f.carry, forced=f.forced.visuals
    )
    entries = need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if len(entries) < 2:
        return f.clarify('Which visuals should go in a row? Name them, for example "the cards".')
    if len(entries) > MAX_VISUALS:
        return f.clarify(f"I can line up at most {MAX_VISUALS} visuals at a time.")
    ids = [e.id for e in entries]
    edge = "left" if column else "top"
    made: list[Op] = [{"op": "align_visuals", "visuals": ids, "edge": edge, "to": "selection"}]
    axis = "vertical" if column else "horizontal"
    if len(entries) >= 3:
        made.append({"op": "distribute_visuals", "visuals": ids, "axis": axis})
    line = "column" if column else "row"
    lined = "left edges" if column else "tops"
    spaced = " and spaced them evenly" if len(entries) >= 3 else ""
    return f.apply(
        entries,
        made,
        f"Put {names_phrase(entries)} in a {line}: lined up their {lined}{spaced}.",
        how="position",
    )


LAYOUT_RECOGNIZERS = (swap_clause, distribute_clause, row_clause, place_clause, align_clause)

__all__ = [
    "LAYOUT_RECOGNIZERS",
    "align_clause",
    "center_clause",
    "distribute_clause",
    "place_clause",
    "row_clause",
    "swap_clause",
]
