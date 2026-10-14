"""New objects that hold no fields: a text box saying something, a shape.

"add a text box saying Q3 review", "give the page a title", "add a red circle" become
``add_textbox`` and ``add_shape``. Where the object goes is the operation's business (free space
on the page); a request only fixes a spot when it says so in numbers ("at 100, 50") or by a
side of the page ("at the top"). What an existing text box says is its caption, a title, which
Studio does not change: such a request gets the styling refusal.
"""

from __future__ import annotations

import re
from typing import Any

from . import edit_ops as ops
from .edit_context import PageEntry
from .edit_frame import Frame, capability
from .edit_targets import find_page
from .edit_text import (
    find_amounts,
    find_colors,
    find_font,
    mask_quotes,
    plain_number,
    unmask_quotes,
)
from .edit_types import Choice, Clause, ParseKind, Target

Op = dict[str, Any]

MAX_TEXT = 2000  # characters in a text box (what add_textbox takes)

# --- pages ---------------------------------------------------------------------------------------

# A title that shows on the page is a text box; the name on the page tab is a rename.
PAGE_TITLE_REQUEST = re.compile(
    r"\b(?:add|give|put|insert|create|include|need|want)\b.{0,25}?(?:"
    r"\b(?:page|report|dashboard)\s+(?:title|heading|header)\b|"
    r"\b(?:title|heading|header)\b.{0,20}?\b(?:to|on|for|above|at\s+the\s+top\s+of)\s+"
    r"(?:the\s+|this\s+|my\s+|every\s+|each\s+)?(?:[\w'-]+\s+)?(?:pages?|reports?|dashboards?)\b|"
    r"\bpages?\b.{0,10}?\ba\s+(?:title|heading|header)\b)",
    re.I,
)


def _active(f: Frame) -> PageEntry | None:
    context = f.context
    if context.active_page:
        return context.page(context.active_page)
    return context.pages[0] if len(context.pages) == 1 else None


def _page_for(f: Frame) -> PageEntry | None:
    """The page a new object goes to: the one the request names, else the page being edited."""
    named = f.res.page or find_page(f.text, f.context)[0]
    return named or _active(f)


def _ask_page(f: Frame) -> Clause:
    choices = [
        Choice(label=p.name, fill={"append": f"on page {p.index + 1}"}) for p in f.context.pages[:8]
    ]
    return f.clarify("On which page should it go?", choices)


def _result(f: Frame, made: list[Op], summary: str, page: PageEntry) -> Clause:
    target = Target((), page.id, f"page '{page.name}'", "page")
    return Clause(f.text, ParseKind.APPLY, ops=tuple(made), summary=summary, targets=(target,))


# --- where it goes -------------------------------------------------------------------------------

_AT = re.compile(
    r"\bat\s+(?:x\s*[=:]?\s*)?(?P<x>\d+(?:\.\d+)?)\s*(?:px)?\s*(?:,|and|/|;)?\s*(?:y\s*[=:]?\s*)?"
    r"(?P<y>\d+(?:\.\d+)?)\b(?!\s*(?:%|k\b|m\b))",
    re.I,
)
_SIDE = re.compile(
    r"\b(?:at|on|in|near|along)\s+the\s+(?P<where>top|bottom)\b(?!\s+(?:left|right))|"
    r"\bacross\s+the\s+(?P<across>top|bottom)\b",
    re.I,
)
_CORNER = re.compile(
    r"\b(?:in|at|on)\s+the\s+(?P<v>top|bottom)[\s-]+(?P<h>left|right)(?:\s+corner)?\b", re.I
)
_SIZE = re.compile(
    r"(?<![\d.])(\d{2,5}(?:\.\d+)?)\s*(?:px)?\s*(?:x|×|by)\s*(\d{2,5}(?:\.\d+)?)(?![\d.])", re.I
)
STRIP_HEIGHT = 50.0  # a text box across the top or bottom of the page
# The receipt of an object whose place the request described in words the parser cannot turn into
# a position yet: it is in free space, and the person is told rather than left to find out.
SPOT_NOT_HONORED = (
    " in free space (I cannot place it by words like that yet; move it where you want it)"
)


def explicit_box(text: str) -> Op:
    """The position and size a request gives in numbers: "at 100, 50", "300 x 80". A new object
    is placed by the operation (in free space) unless the person says where."""
    found: Op = {}
    size = _SIZE.search(text)
    if size:
        found.update(width=float(size.group(1)), height=float(size.group(2)))
    at = _AT.search(text)
    if at:
        found.update(x=float(at.group("x")), y=float(at.group("y")))
    return found


def _placement(page: PageEntry, text: str) -> tuple[Op, str]:
    """(the position and size the request fixes, a phrase for the receipt). Numbers are taken as
    said; "at the top" is a strip across the page; a top left corner is a position. Anything
    else is left to the operation's search for free space."""
    found = explicit_box(text)
    if "x" in found:
        return found, f" at {plain_number(found['x'])}, {plain_number(found['y'])}"
    if found:
        return found, ""
    margin = ops.gap(page)
    corner = _CORNER.search(text)
    if corner:
        # only the top left corner is a spot that needs no size; the others are left to the
        # operation, which finds free space
        if corner.group("v").casefold() == "top" and corner.group("h").casefold() == "left":
            return {"x": margin, "y": margin}, " in the top left corner"
        return {}, ""
    side = _SIDE.search(text)
    if side:
        where = (side.group("where") or side.group("across")).casefold()
        return (
            {
                "x": margin,
                "y": margin if where == "top" else round(page.height - margin - STRIP_HEIGHT, 2),
                "width": round(page.width - 2 * margin, 2),
                "height": STRIP_HEIGHT,
            },
            f" across the {where} of the page",
        )
    return {}, ""


# --- a text box ----------------------------------------------------------------------------------

_ADD = r"(?:add|insert|create|put|place|draw|make|give\s+me|include|write|type)"
_TEXTBOX = re.compile(
    rf"\b{_ADD}\s+(?:me\s+)?(?:a|an|another|one|some|the|new)?\s*(?:new\s+)?(?:[\w-]+\s+){{0,4}}?"
    r"text\s?box(?:es)?\b",
    re.I,
)
_NOTE = re.compile(
    rf"\b{_ADD}\s+(?:me\s+)?(?:a|an|another|one|some)\s+(?:new\s+)?(?:short\s+)?"
    r"(?:note|annotation|subtitle|sub-title|footnote|banner\s+text)\b",
    re.I,
)
# "with the title of the report" says what the box is about, not what it says: only these words
# introduce the text itself
_SAYING = re.compile(
    r"(?:\b(?:saying|says?|reading|reads?|containing|contains?|with\s+the\s+(?:text|words|message)|"
    r"with\s+text|titled|called|named|labell?ed)\b\s*:?\s*|:\s*)(?P<text>.+)$",
    re.I | re.S,
)
# what follows the words of a request for a text box, when they were not quoted
_PLACE_PHRASE = (
    r"(?:below|above|under|underneath|beneath|beside|next\s+to|near|to\s+the\s+(?:left|right)\s+of|"
    r"in\s+the\s+(?:top|bottom)[\s-]+(?:left|right|middle|centre|center)|"
    r"in\s+the\s+(?:middle|center|centre|corner)|on\s+the\s+(?:left|right)(?:[\s-]+side)?|"
    r"at\s+the\s+(?:left|right)(?:[\s-]+side)?)"
)
PLACE_WORDS = re.compile(
    rf"\b(?:{_PLACE_PHRASE}|(?:at|on|in|along)\s+the\s+(?:top|bottom))\b", re.I
)
_PLACE_WORDS = PLACE_WORDS
_TRAILING = re.compile(
    rf"\s+(?:{_PLACE_PHRASE}\b.*|at\s+-\s*\d.*|"
    r"(?:at|on|in|near|along|across)\s+the\s+(?:top|bottom)\b.*|"
    r"(?:on|to|onto|in)\s+(?:the\s+)?(?:[\w'-]+\s+){0,2}?(?:page|tab)\b.*|"
    r"at\s+(?:x\s*[=:]?\s*)?\d.*|(?:in|with)\s+(?:bold|italic|size\s+\d+\b.*|\d+\s*pt\b.*)|"
    r"(?:and|then)\s+(?:make|put|move|set|color|colour)\b.*)$",
    re.I,
)


def _quoted_text(text: str) -> str | None:
    guarded, saved = mask_quotes(text)
    found = re.search(r"\x01\d+\x01", guarded)
    if found is None:
        return None
    inner = re.sub(r"^[\"']|[\"']$", "", unmask_quotes(found.group(0), saved))
    return inner.strip() or None


def _box_text(text: str) -> str | None:
    """What the person wants the text box to say: quoted text, or the words after "saying"."""
    said = _SAYING.search(text)
    remainder = ""
    if said:
        remainder = said.group("text").strip().rstrip(".!?").strip()
        guarded, saved = mask_quotes(remainder)
        guarded = _TRAILING.sub("", guarded).strip().rstrip(".!?").strip()
        remainder = unmask_quotes(guarded, saved)
    quoted = _quoted_text(remainder or text)
    if quoted:
        # "saying Q3 "final" review": the quotes are part of the words when other words come
        # with them; only a quote that is the whole text marks it off
        whole = remainder.strip(" \"'")
        if not remainder or whole == quoted or remainder.strip() in {f'"{quoted}"', f"'{quoted}'"}:
            return quoted
        return " ".join(remainder.split()).strip() or quoted
    return remainder.strip(" \"'") or None


def _shown(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= 60 else line[:59].rstrip() + "…"


def _text_box(f: Frame) -> Clause | None:
    text = f.text
    words_box = _TEXTBOX.search(text)
    title_page = PAGE_TITLE_REQUEST.search(f.low)
    if not (words_box or _NOTE.search(text) or title_page):
        return None
    page = _page_for(f)
    if page is None:
        return _ask_page(f)
    said = _box_text(text)
    if said is None:
        what = "title" if title_page and not words_box else "text box"
        return f.clarify(
            f'What should the {what} say? For example "add a text box saying Q3 review".'
        )
    if len(said) > MAX_TEXT:
        return f.clarify(f"A text box can hold at most {MAX_TEXT} characters. Say it more briefly.")
    # placement, color and size come from the words around the text, never from the text itself
    outside = text.replace(said, " ")
    if re.search(r"\bat\s+(?:x\s*[=:]?\s*)?-\s*\d", outside, re.I):
        return f.clarify(
            "A position on the page starts at 0, 0 in the top left corner. Where should it go?"
        )
    made: Op = {"op": "add_textbox", "page": page.id, "text": said}
    placed, where = _placement(page, outside)
    if title_page and not words_box and not placed:
        placed, where = _placement(page, f"{outside} at the top")
    made.update(placed)
    if not placed and _PLACE_WORDS.search(outside):
        where = SPOT_NOT_HONORED
    size = next(
        (
            a
            for a in find_amounts(outside)
            if a.unit == "pt" or re.search(r"\bsize\s*$", outside[max(0, a.start - 6) : a.start])
        ),
        None,
    )
    if size is not None and 6 <= size.value <= 72:
        made["font_size"] = size.value
    if re.search(r"\bbold\b", outside, re.I):
        made["bold"] = True
    family = find_font(outside)
    if family:
        made["font_family"] = family
    colors = find_colors(outside, f.context.palette)
    if colors:
        made["color"] = colors[0].hex
    summary = f"Added a text box saying '{_shown(said)}' to page '{page.name}'{where}."
    return _result(f, [made], summary, page)


_CHANGE_TEXT = re.compile(
    r"\b(?:change|edit|update|replace|rewrite|set|make|fix)\s+(?:the\s+|that\s+|this\s+)?"
    r"(?:[\w-]+\s+){0,2}?text\s?box(?:es)?\b.*?\b(?:to\s+(?:say|read)|so\s+(?:that\s+)?it\s+"
    r"(?:says|reads)|say|read|saying)\b\s*:?\s*(?P<text>.+)$|"
    r"\b(?:change|edit|update|replace|rewrite)\s+the\s+text\s+(?:in|of|inside)\s+"
    r"(?:the\s+|that\s+|this\s+)?(?:[\w-]+\s+){0,2}?text\s?box(?:es)?\s+to\s+(?P<text2>.+)$",
    re.I | re.S,
)


def _edit_text_box(f: Frame) -> Clause | None:
    """ "change the text box to say Summer sale": the words of an existing text box are its
    caption, a title, which Studio does not change (the styling refusal). A new text box is
    ``add_textbox``."""
    if _CHANGE_TEXT.search(f.text) is None:
        return None
    return f.unsupported(capability("styling"))


# --- a shape -------------------------------------------------------------------------------------

_SHAPES: tuple[tuple[str, str], ...] = (
    (r"rounded\s+rectangles?|rounded\s+squares?", "rectangleRounded"),
    (r"rectangles?|squares?", "rectangle"),
    (r"circles?|ovals?|ellipses?", "oval"),
    (r"right[\s-]+triangles?", "triangleRight"),
    (r"triangles?", "triangleIsoc"),
    (r"arrows?", "arrow"),
    (r"chevrons?", "arrowChevron"),
    (r"pentagons?", "pentagon"),
    (r"hexagons?", "hexagon"),
    (r"octagons?", "octagon"),
    (r"hearts?", "heart"),
    (r"pills?", "pill"),
    (r"trapezoids?", "trapezoid"),
    (r"parallelograms?", "parallelogram"),
    (r"speech\s+bubbles?|callouts?", "speechbubbleRectangle"),
    (r"lines?(?!\s+(?:chart|graph|plot|series|visual))", "line"),
)
_SHAPE_WORDS = "|".join(pattern for pattern, _ in _SHAPES)
_SHAPE_NAMES = {
    "rectangleRounded": "rounded rectangle",
    "triangleIsoc": "triangle",
    "triangleRight": "right triangle",
    "arrowChevron": "chevron",
    "speechbubbleRectangle": "speech bubble",
}
_ADD_SHAPE = re.compile(
    r"\b(?:add|insert|draw|create|put|place|include|make)\s+(?:me\s+)?(?:a|an|another|one|some)\s+"
    rf"(?:new\s+)?(?P<lead>(?:[\w#-]+\s+){{0,4}}?)(?P<shape>{_SHAPE_WORDS}|shapes?)\b",
    re.I,
)
_SHAPE_WORDS_INSIDE = re.compile(
    r"\b(?:with\s+(?:the\s+)?(?:text|words|label|caption)|saying|that\s+says|containing|"
    r"reading)\b\s*:?\s*(?P<text>.+)$",
    re.I | re.S,
)
_COLOR_WORDS = r"(?:[a-z]+[\s-]+)?[a-z#0-9]+"
_OUTLINE = re.compile(
    rf"(?:\b(?:with|and)\s+(?:an?\s+)?|\bin\s+)(?P<color>{_COLOR_WORDS})\s+"
    rf"(?:outline|border|stroke|edge)\b|\b(?:outlined|bordered|stroked)\s+(?:in|with)\s+"
    rf"(?P<color2>{_COLOR_WORDS})\b",
    re.I,
)
_FILL = re.compile(
    rf"\b(?:filled|fill(?:ing)?)\s+(?:it\s+|them\s+)?(?:in|with)\s+(?P<color>{_COLOR_WORDS})", re.I
)
EQUAL_SIDES = 120.0  # a circle or a square: both sides the same


def _shape_key(word: str) -> str:
    for pattern, key in _SHAPES:
        if re.fullmatch(pattern, word.strip(), re.I):
            return key
    return "rectangle"


def _shape(f: Frame) -> Clause | None:
    found = _ADD_SHAPE.search(f.text)
    if found is None or re.search(r"\btext\s*$", found.group("lead"), re.I):
        return None
    word = found.group("shape").casefold()
    key = "rectangle" if re.fullmatch(r"shapes?", word) else _shape_key(word)
    page = _page_for(f)
    if page is None:
        return _ask_page(f)
    words = _SHAPE_WORDS_INSIDE.search(f.text)
    if words:
        # a shape holds no text: say so instead of dropping the words
        said = _quoted_text(words.group("text")) or words.group("text").strip().strip(" \"'.!?")
        if said:
            return f.clarify(
                "A shape cannot hold text. Add a text box saying "
                f"'{_shown(said)}' instead (place it over the shape)?",
                [
                    Choice(
                        label="Yes, add a text box",
                        ops=({"op": "add_textbox", "page": page.id, "text": said},),
                        summary=f"Added a text box saying '{_shown(said)}' to page '{page.name}'.",
                    )
                ],
            )
    palette = f.context.palette
    made: Op = {"op": "add_shape", "page": page.id, "shape": key}
    outline = _OUTLINE.search(f.text)
    outline_color = (
        find_colors(outline.group("color") or outline.group("color2") or "", palette)
        if outline
        else []
    )
    if outline_color:
        made["outline"] = outline_color[0].hex
    # a fill is the color in front of the shape or after "filled with", outside the outline
    outside = (
        f.text if outline is None else f"{f.text[: outline.start()]} {f.text[outline.end() :]}"
    )
    filled = _FILL.search(outside)
    fill_color = find_colors(filled.group("color") if filled else found.group("lead"), palette)
    if fill_color:
        made["fill"] = fill_color[0].hex
    placed, where = _placement(page, f.text)
    made.update(placed)
    if not placed and _PLACE_WORDS.search(f.text):
        where = SPOT_NOT_HONORED
    equal = re.fullmatch(r"circles?|squares?", word) is not None
    if equal and "width" not in made:
        made["width"] = made["height"] = EQUAL_SIDES
    name = "circle" if equal and key == "oval" else "square" if equal else None
    name = name or _SHAPE_NAMES.get(key, key)
    look = f"{fill_color[0].text.casefold()} " if fill_color else ""
    article = "an" if (look or name)[:1] in "aeiou" else "a"
    edge = f" with a {outline_color[0].text.casefold()} outline" if outline_color else ""
    summary = f"Added {article} {look}{name} shape to page '{page.name}'{edge}{where}."
    return _result(f, [made], summary, page)


def add_object_clause(f: Frame) -> Clause | None:
    """A text box or a shape to add, or the words of a text box to change."""
    return _edit_text_box(f) or _text_box(f) or _shape(f)


__all__ = [
    "PAGE_TITLE_REQUEST",
    "PLACE_WORDS",
    "SPOT_NOT_HONORED",
    "add_object_clause",
    "explicit_box",
]
