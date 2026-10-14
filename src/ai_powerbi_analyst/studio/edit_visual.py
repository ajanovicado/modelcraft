"""Recognizers for edits to one or more visuals: layout and structure, and the styling refusal.

Each recognizer reads a ``Frame`` (the clause, its resolved target) and answers with a
Clause, or None when the clause is not its kind. Styling is read by slots, the way the Format
pane is organised: the text parts a request names (title, legend, data labels, the bars...) and
the values it gives them (a color, a size, on or off). Studio does not change styling, so a
request that gives one of them a value is answered with the styling refusal
(``styling_clause``); no operation is built for it. Size words that name no text part ("make
it bigger") are the visual's own size (``resize_clause``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import edit_ops as ops
from .edit_context import PageEntry, VisualEntry
from .edit_frame import Forced, Frame, capability, names_phrase
from .edit_frame import need_entries as _need_entries
from .edit_layout import LAYOUT_RECOGNIZERS, center_clause
from .edit_targets import (
    find_page,
    find_types,
    resolve_visuals,
)
from .edit_text import (
    FIELD_OR_MODEL,
    Amount,
    ColorMatch,
    find_amounts,
    find_colors,
    find_font,
    mask_quotes,
    plain_number,
    unmask_quotes,
)
from .edit_text import PROPERTY_NOUNS as _PROPERTY_NOUNS
from .edit_types import Choice, Clause

Op = dict[str, Any]

# --- parts -------------------------------------------------------------------------------------

_TAIL = r"(?:\s+(?:font|text|colou?r|size|style))*"
_PARTS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.I))
    for name, pattern in (
        (
            "title_background",
            r"\b(?:title|heading|caption)\s+background\b|\bbackground\s+(?:of|for)\s+(?:the\s+)?"
            r"(?:title|heading|caption)\b",
        ),
        (
            "axis_title",
            r"\b(?:(?:x|y|horizontal|vertical|category|value)[\s-]*)?axis\s+titles?\b|"
            r"\baxes\s+titles?\b|\btitles?\s+(?:of|on|for)\s+(?:the\s+)?(?:(?:x|y)[\s-]*)?"
            r"ax(?:is|es)\b",
        ),
        ("title", rf"\b(?:titles?|headings?|captions?){_TAIL}\b"),
        ("legend", rf"\blegends?{_TAIL}\b"),
        (
            "x_axis",
            rf"\b(?:x|horizontal|category)[\s-]*axis(?:\s+(?:labels?|numbers?))?{_TAIL}\b|"
            # the category names under the bars are the x axis's labels, not data labels
            rf"\bcategory\s+(?:labels?|names?){_TAIL}\b",
        ),
        (
            "y_axis",
            rf"\b(?:y|vertical|value)[\s-]*axis(?:\s+(?:labels?|numbers?))?{_TAIL}\b",
        ),
        ("axis", rf"\baxes\b|\baxis(?:\s+labels?)?{_TAIL}\b"),
        ("data_labels", rf"\b(?:(?:data|value)\s+)?labels?{_TAIL}\b"),
        ("gridlines", r"\bgrid\s*lines?\b"),
        ("background", r"\bbackgrounds?\b"),
        ("border", r"\b(?:borders?|outlines?|frames?)\b"),
        ("shadow", r"\bshadows?\b"),
        (
            "corners",
            r"\b(?:rounded?\s+corners?|square\s+corners?|corners?|rounded|rounding|round\s+off|"
            r"radius|round)\b",
        ),
        ("transparency", r"\btranspar\w*|\bopacity\b|\bsee[- ]through\b"),
        ("headers", rf"\b(?:column\s+)?headers?{_TAIL}\b"),
        ("values", rf"\b(?:values?|numbers?|figures?|digits?|callouts?){_TAIL}\b"),
        (
            "data",
            r"\b(?:bars?|columns|lines|slices|series|data\s*points?|points|dots|bubbles|"
            r"markers|areas?|fill|segments?|wedges?)\b",
        ),
        ("text", rf"\b(?:fonts?(?:\s+size)?|typeface|text|lettering|writing){_TAIL}\b"),
        ("color_generic", r"\bcolou?rs?\b"),
    )
)
TEXT_PARTS = {
    "title",
    "data_labels",
    "legend",
    "x_axis",
    "y_axis",
    "axis",
    "values",
    "headers",
    "text",
}
COLOR_PARTS = TEXT_PARTS | {"title_background", "background", "border", "data", "color_generic"}
TOGGLE_PARTS = {
    "title",
    "data_labels",
    "legend",
    "x_axis",
    "y_axis",
    "axis",
    "gridlines",
    "axis_title",
    "border",
    "shadow",
    "background",
}


@dataclass(frozen=True)
class Part:
    name: str
    start: int
    end: int


def find_parts(m: str) -> list[Part]:
    found: list[Part] = []
    for name, pattern in _PARTS:
        for match in pattern.finditer(m):
            span = (match.start(), match.end())
            if any(span[0] < p.end and p.start < span[1] for p in found):
                continue
            found.append(Part(name, *span))
    return sorted(found, key=lambda p: p.start)


_OFF = re.compile(
    r"\b(?:remove|delete|drop|erase|hide|disable|without|get\s+rid\s+of|take\s+out|clear|"
    r"ditch|scrap|zap|kill|toss|lose|"
    r"stop\s+showing|(?:don'?t|do\s+not|not)\s+(?:show|display|need|want)|no(?:t)?\s+(?:more\s+)?"
    r"(?=(?:a\s+|the\s+)?(?:legend|title|labels?|grid|border|shadow|axis|background|data)))\b|"
    r"\b(?:turn|switch|toggle)\b.{0,30}?\boff\b|\boff\s*$|\bno\s+(?=[a-z])",
    re.I,
)
_ON = re.compile(
    r"\b(?:add|show|display|enable|include|insert|bring\s+back|turn\s+on|switch\s+on|"
    r"with\s+(?:a|the)?|give\s+(?:it\s+|them\s+)?(?:a|the)?|wants?\s+(?:a|the)|"
    r"needs?|requires?|has|have|having)\b|\b(?:turn|switch|toggle)\b.{0,30}?\bon\b|\bon\s*$",
    re.I,
)


def toggle_of(m: str) -> bool | None:
    """True for add/show/turn on, False for remove/hide/turn off, None for neither."""
    # "add 2 pt to the title font" adds points, not a title
    m = re.sub(r"\badd\s+(?=[+-]?\d)", "increase ", m)
    # "drop shadow" is a shadow; "on" before a name the resolver blanked is a place ("hide the
    # x axis on <chart>"), not "turn it on"
    m = re.sub(r"\bdrop[\s-]+(?=shadows?\b)", "", m)
    m = re.sub(r"\bon\s+$", " ", m) if re.search(r"\bon\s{2,}$", m) else m
    off, on = _OFF.search(m), _ON.search(m)
    if off and on and off.start() <= on.start() and on.end() <= off.end():
        on = None  # the "show" of "don't show" is part of the off phrase
    if off and on:
        # "should have no gridlines": the word that comes last decides
        return on.start() > off.start()
    if off:
        return False
    if on:
        return True
    return None


_LEGEND_POSITIONS = {
    "top right": "TopRight",
    "top center": "TopCenter",
    "top centre": "TopCenter",
    "bottom right": "BottomRight",
    "bottom center": "BottomCenter",
    "bottom centre": "BottomCenter",
    "left center": "LeftCenter",
    "right center": "RightCenter",
    "top": "Top",
    "bottom": "Bottom",
    "left": "Left",
    "right": "Right",
    "above": "Top",
    "below": "Bottom",
    "underneath": "Bottom",
    "under": "Bottom",
}
_LABEL_POSITIONS = {
    "outside end": "OutsideEnd",
    "outside": "OutsideEnd",
    "inside end": "InsideEnd",
    "inside base": "InsideBase",
    "inside center": "InsideCenter",
    "inside centre": "InsideCenter",
    "inside": "InsideEnd",
    "center": "InsideCenter",
    "centre": "InsideCenter",
    "above": "Above",
    "on top": "Above",
    "under": "Under",
    "below": "Under",
    "auto": "Auto",
}


def _phrase_position(m: str, table: dict[str, str]) -> str | None:
    for words in sorted(table, key=len, reverse=True):
        if re.search(rf"\b{words}\b", m):
            return table[words]
    return None


_UNITS = (
    (re.compile(r"\b(?:thousands?|in\s+k|in\s+thousand)\b"), "thousands"),
    (re.compile(r"\b(?:millions?|in\s+m)\b"), "millions"),
    (re.compile(r"\b(?:billions?|in\s+bn|in\s+b)\b"), "billions"),
    (re.compile(r"\b(?:no\s+units?|full\s+numbers?|exact\s+numbers?|not\s+abbreviated)\b"), "none"),
    (re.compile(r"\bauto(?:matic(?:ally)?)?\s+units?\b"), "auto"),
)
_DECIMALS = re.compile(
    r"\b(\d+)\s+decimal(?:s|\s+places?)?\b|\b(?:no|zero)\s+decimals?\b|\bwhole\s+numbers?\b|"
    r"\bdecimals?\s*(?:to|of|at|=|:)\s*(\d+)\b|\bround(?:ed)?\s+to\s+(\d+)\s+(?:decimals?|places?)\b",
    re.I,
)


def label_units(m: str) -> str | None:
    for pattern, name in _UNITS:
        if pattern.search(m):
            return name
    return None


def label_decimals(m: str) -> int | None:
    match = _DECIMALS.search(m)
    if not match:
        return None
    digits = next((g for g in match.groups() if g), None)
    return int(digits) if digits is not None else 0


_ALIGN = re.compile(r"\b(?:(left|right|center|centre|centered|centred)(?:[\s-]*align(?:ed)?)?)\b")


def title_alignment(m: str) -> str | None:
    match = _ALIGN.search(m)
    if not match:
        return None
    word = match.group(1)
    return "center" if word.startswith("cent") else word


# --- slots -------------------------------------------------------------------------------------

_GROW_UP = re.compile(
    r"\b(?:bigger|larger|greater|increase[sd]?|enlarge[sd]?|grow|expand|boost|bump(?:\s+up)?|"
    r"raise(?=\s+(?:the\s+)?(?:[\w-]+\s+){0,2}?(?:font|text|size))|upsize|magnify|"
    r"more\s+prominent|bolder\s+size)\b",
    re.I,
)
# "lower the StoreName slicer" moves it down; only "lower the font size" makes something smaller.
_GROW_DOWN = re.compile(
    r"\b(?:smaller|tinier|shrink|reduce[sd]?|decrease[sd]?|downsize|less\s+prominent|"
    r"lower(?=\s+(?:the\s+)?(?:[\w-]+\s+){0,2}?(?:font|text|size))|diminish)\b",
    re.I,
)
_SLIGHT = re.compile(
    r"\b(?:slightly|a\s+(?:bit|little|touch|tad)|little|bit|tiny\s+bit|marginally)\b", re.I
)
_MUCH = re.compile(
    r"\b(?:much|way|a\s+lot|lots|significantly|far|very|really|considerably|huge(?:ly)?)\b", re.I
)
_BOLD = re.compile(r"(?<!un)\b(?:bold(?:er|ed)?|heavy|heavier)\b", re.I)
_UNBOLD = re.compile(
    r"\b(?:not\s+bold|unbold|remove\s+(?:the\s+)?bold(?:ing)?|no\s+bold|non[- ]bold)\b", re.I
)
_ITALIC = re.compile(r"\bitalics?\b|\bitalici[sz]e\b|\bslanted\b", re.I)
_UNITALIC = re.compile(
    r"\b(?:not\s+italic|unitalic|no\s+italics?|remove\s+(?:the\s+)?italics?)\b", re.I
)


_DARKER = re.compile(
    r"\b(?:darker|deeper|dimmer|richer|more\s+saturated|darken(?:ed)?|dim)\b", re.I
)
_LIGHTER = re.compile(
    r"\b(?:lighter|paler|brighter|softer|fainter|more\s+muted|less\s+intense|lighten(?:ed)?|"
    r"brighten(?:ed)?|soften(?:ed)?)\b",
    re.I,
)


@dataclass
class Slots:
    parts: list[Part]
    colors: list[ColorMatch]
    shade: float | None
    size: float | None  # an absolute font size
    delta: float | None  # a change in points
    grow: int  # +1 bigger, -1 smaller, 0 neither
    degree: float  # 1 by default, 0.5 "a bit", 3 "much"
    bold: bool | None
    italic: bool | None
    family: str | None
    toggle: bool | None
    units: str | None
    decimals: int | None
    consumed: list[tuple[int, int]] = field(default_factory=list)


_FONTISH = re.compile(
    r"\b(?:font|text|size|points?|pts?|titles?|headings?|labels?|legend|axis|axes|headers?|"
    r"values?|numbers?)\b",
    re.I,
)
_NOT_A_FONT_SIZE = re.compile(
    r"\s*(?:px|pixels?|%|percent|wide|width|tall|high|height|decimals?|x\b|\u00d7)", re.I
)
_CHANGE_VERB = re.compile(
    r"\b(?:add|increase|decrease|reduce|raise|lower|bump|boost|grow|shrink|enlarge|plus|minus)\b",
    re.I,
)
_SHRINK_VERB = re.compile(r"\b(?:decrease|reduce|lower|shrink|minus|smaller|less)\b", re.I)
_COMPARATIVE_AFTER = re.compile(
    r"\s*(bigger|larger|greater|smaller|tinier|less|more|up|down)\b", re.I
)
_SMALLER_WORDS = frozenset({"smaller", "tinier", "less", "down"})


def _font_amounts(m: str, amounts: list[Amount]) -> tuple[float | None, float | None]:
    """(absolute size, change in points) from numbers near font words: "16pt", "size 14",
    "set the font size of the slicers to 12", "by 2 pt", "+2", "add 2 pt"."""
    size = delta = None
    fontish = bool(_FONTISH.search(m))
    for amount in amounts:
        if amount.unit not in {"", "pt"}:
            continue
        before = m[max(0, amount.start - 24) : amount.start]
        after = m[amount.end : amount.end + 14]
        if amount.unit == "":
            if not fontish or _NOT_A_FONT_SIZE.match(after):
                continue
            if re.search(r"(?:\bx|\u00d7)\s*[=:]?\s*$", before) or re.search(
                r"\by\s*[=:]?\s*$", before
            ):
                continue
        to_value = bool(re.search(r"\b(?:to|at|=|:|is)\s*$", before))
        by_before = bool(re.search(r"\bby\s*$", before))
        # "5 pt smaller" is a change of 5, whatever the size is now ("40 pt smaller" must not
        # become a 40 pt font)
        comparative = _COMPARATIVE_AFTER.match(after) if fontish and not to_value else None
        relative = (
            amount.signed
            or by_before
            or comparative is not None
            or (bool(_CHANGE_VERB.search(m[: amount.start])) and not to_value)
        )
        if relative:
            if comparative is not None and not amount.signed:
                # each amount carries its own direction, so "2 pt smaller and then 2 pt bigger"
                # adds up instead of the first word deciding both
                shrinking = comparative.group(1).casefold() in _SMALLER_WORDS
                step = -abs(amount.value) if shrinking else abs(amount.value)
                delta = step if delta is None else delta + step
                continue
            shrinking = amount.value < 0 or (
                not amount.signed and bool(_SHRINK_VERB.search(m[: amount.start]))
            )
            delta = -abs(amount.value) if shrinking else abs(amount.value)
        elif 6 <= amount.value <= 72:  # Power BI's font sizes; "2" in "need 2 legends" is not one
            size = amount.value
    return size, delta


def read_slots(f: Frame, m: str | None = None) -> Slots:
    text = m if m is not None else f.masked
    context = f.context
    colors = find_colors(text, context.palette)
    amounts = find_amounts(text)
    size, delta = _font_amounts(text, amounts)
    grow = 1 if _GROW_UP.search(text) else -1 if _GROW_DOWN.search(text) else 0
    if grow == 1 and _GROW_DOWN.search(text):
        grow = 0
    degree = 0.5 if _SLIGHT.search(text) else 3.0 if _MUCH.search(text) else 1.0
    bold = False if _UNBOLD.search(text) else True if _BOLD.search(text) else None
    italic = False if _UNITALIC.search(text) else True if _ITALIC.search(text) else None
    strength = {0.5: 0.15, 1.0: 0.3, 3.0: 0.5}[degree]
    shade = -strength if _DARKER.search(text) else strength if _LIGHTER.search(text) else None
    if colors:
        shade = None  # "a darker blue" is a color, not a change to the current one
    return Slots(
        parts=find_parts(text),
        colors=colors,
        shade=shade,
        size=size,
        delta=delta,
        grow=grow,
        degree=degree,
        bold=bold,
        italic=italic,
        family=find_font(text),
        toggle=toggle_of(text),
        units=label_units(text),
        decimals=label_decimals(text),
    )


# --- helpers shared by recognizers -----------------------------------------------------------


def _phrase(entries: list[VisualEntry]) -> str:
    return names_phrase(entries)


def _page_of(f: Frame, entry: VisualEntry) -> PageEntry:
    return ops.page_of(f.context, entry)


# --- structure: fields and sort ----------------------------------------------------------------

_FIELD_ROLE = re.compile(
    r"\b(?:on|in|as|to|into|for)\s+(?:the\s+)?(?P<role>rows?|columns?|values?|legend|"
    r"x[\s-]?axis|y[\s-]?axis|axis|tooltips?|small\s+multiples|details?|category|series)\b",
    re.I,
)
_ROLE_NAMES = {
    "row": "Rows",
    "rows": "Rows",
    "column": "Columns",
    "columns": "Columns",
    "value": "Values",
    "values": "Values",
    "legend": "Legend",
    "x axis": "X-axis",
    "xaxis": "X-axis",
    "y axis": "Y-axis",
    "yaxis": "Y-axis",
    "axis": "X-axis",
    "tooltip": "Tooltips",
    "tooltips": "Tooltips",
    "small multiples": "Small multiples",
    "detail": "Details",
    "details": "Details",
    "category": "Category",
    "series": "Legend",
}
_FIELD_VERB_ADD = re.compile(r"\b(?:add|include|put|drag|bring|insert|use|show|plot)\b", re.I)
_FIELD_VERB_REMOVE = re.compile(
    r"\b(?:remove|delete|drop|take\s+out|get\s+rid\s+of|ditch|scrap|zap|kill|toss|lose|clear|"
    r"stop\s+showing)\b",
    re.I,
)
_FIELD_VERB_REPLACE = re.compile(r"\b(?:replace|swap)\b.+\bwith\b", re.I)


def fields_clause(f: Frame) -> Clause | None:
    """Fields: "add Profit to the table", "remove Rank from the chart", "replace A with B in it"."""
    hits = [
        h
        for h in ops.match_fields(f.text, f.context)
        # "the ChannelName slicer" names a visual by its field, it is not a field to add
        if not re.match(
            r"\s+(?:slicers?|cards?|charts?|graphs?|tables?|matri\w+|visuals?|kpis?)\b",
            f.text[h.end :],
            re.I,
        )
    ]
    if not hits:
        return None
    low = f.low
    remove = _FIELD_VERB_REMOVE.search(low)
    replace = _FIELD_VERB_REPLACE.search(low)
    add = _FIELD_VERB_ADD.search(low)
    prep = re.search(
        r"\b(?:to|into|in|on|from|out\s+of|of)\s+(?:the\s+|this\s+|that\s+|my\s+)?"
        r"(?:[\w'-]+\s+){0,3}?(?:chart|graph|table|matrix|card|visual|slicer|kpi|gauge|"
        r"map|it|them|this|that)\b",
        low,
    )
    if not (remove or replace or add) or not prep:
        return None
    if (
        add
        and not (remove or replace)
        and re.search(
            r"\b(?:add|create|insert|build|make)\s+(?:a\s+|an\s+|another\s+|new\s+)+(?:new\s+)?"
            r"(?:\w+[\s-]+){0,2}(?:chart|graph|table|matrix|card|slicer|visual|kpi|gauge)\b",
            low,
        )
    ):
        return None  # that adds a visual, not a field
    spans = [(h.start, h.end) for h in hits]
    res = resolve_visuals(
        f.text,
        f.context,
        carry=f.carry,
        exclude=spans,
        forced=f.forced.visuals,
    )
    f.res = res
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if len(entries) != 1:
        return f.clarify(
            "Which visual should change its fields? " + names_phrase(entries),
            [Choice(label=e.label, fill={"visual": e.id}) for e in entries[:6]],
        )
    entry = entries[0]
    role_match = _FIELD_ROLE.search(low)
    role = (
        _ROLE_NAMES.get(re.sub(r"[\s-]+", " ", role_match.group("role").casefold()))
        if role_match
        else None
    )
    made: list[Op] = []
    notes: list[str] = []
    try:
        if replace:
            first, second = hits[0], hits[-1]
            if first is second:
                return None
            source_role = ops.role_of_field(entry, first.name)
            if source_role is None:
                raise ops.Unfit(f"{entry.label} does not show {first.name}")
            made.append(
                {
                    "op": "set_fields",
                    "visual": entry.id,
                    "role": source_role,
                    "mode": "remove",
                    "fields": [first.ref()],
                }
            )
            made.append(
                {
                    "op": "set_fields",
                    "visual": entry.id,
                    "role": source_role,
                    "mode": "add",
                    "fields": [second.ref()],
                }
            )
            summary = f"Replaced {first.name} with {second.name} in {entry.label}."
        elif remove:
            for hit in hits:
                found = ops.role_of_field(entry, hit.name)
                if found is None:
                    raise ops.Unfit(f"{entry.label} does not show {hit.name}")
                made.append(
                    {
                        "op": "set_fields",
                        "visual": entry.id,
                        "role": found,
                        "mode": "remove",
                        "fields": [hit.ref()],
                    }
                )
            names = " and ".join(h.name for h in hits)
            summary = f"Removed {names} from {entry.label}."
        else:
            for hit in hits:
                target_role = role or ops.default_role(entry, hit.kind)
                made.append(
                    {
                        "op": "set_fields",
                        "visual": entry.id,
                        "role": target_role,
                        "mode": "add",
                        "fields": [hit.ref()],
                    }
                )
                notes.append(
                    f"{hit.name} to the {ops.role_display(entry.visual_type, target_role).lower()}"
                )
            summary = f"Added {' and '.join(notes)} of {entry.label}."
    except ops.Unfit as error:
        return f.not_available(str(error))
    return f.apply([entry], made, summary)


_SORT = re.compile(r"\b(?:sort(?:ed)?|order(?:ed)?\s+by|rank(?:ed)?\s+by|arrange\s+by)\b", re.I)
_ASC = re.compile(
    r"\b(?:asc(?:ending)?|a\s*(?:to|-|→)\s*z|alphabetic\w*|lowest\s+(?:to|first)|smallest\s+first|"
    r"low\s+to\s+high|oldest\s+first|least\s+to\s+most|increasing)\b",
    re.I,
)
_DESC = re.compile(
    r"\b(?:desc(?:ending)?|z\s*(?:to|-|→)\s*a|highest(?:\s+(?:to|first))?|largest(?:\s+first)?|"
    r"biggest(?:\s+first)?|high\s+to\s+low|newest\s+first|most\s+to\s+least|decreasing|best\s+first)\b",
    re.I,
)


def sort_clause(f: Frame) -> Clause | None:
    if not _SORT.search(f.low):
        return None
    hits = ops.match_fields(f.text, f.context)
    spans = [(h.start, h.end) for h in hits]
    f.res = resolve_visuals(
        f.text, f.context, carry=f.carry, exclude=spans, forced=f.forced.visuals
    )
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if len(entries) != 1:
        return f.clarify(
            "Which visual should be sorted? " + names_phrase(entries),
            [Choice(label=e.label, fill={"visual": e.id}) for e in entries[:6]],
        )
    entry = entries[0]
    ascending = bool(_ASC.search(f.low)) and not _DESC.search(f.low)
    descending = bool(_DESC.search(f.low))
    on_visual = list(entry.bindings)
    chosen: Op | None = None
    for hit in hits:
        if any(b[2].casefold() == hit.name.casefold() for b in on_visual):
            chosen = hit.ref()
            break
    if chosen is None and hits:
        return f.not_available(
            f"Add {hits[0].name} to {entry.label} before sorting by it, or pick a field it shows."
        )
    if chosen is None:
        alphabetical = bool(re.search(r"alphabetic|a\s*to\s*z|z\s*to\s*a", f.low))
        wanted = "column" if alphabetical else "measure"
        found = next((b for b in on_visual if b[0] == wanted), None)
        if found is None:
            return f.clarify(
                "Sort by which field? " + ", ".join(b[2] for b in on_visual[:6]),
                [],
                [entry],
            )
        chosen = {"table": found[1], "name": found[2]}
    direction = "ascending" if ascending and not descending else "descending"
    if not ascending and not descending:
        # names read A to Z, numbers read biggest first
        is_column = any(
            b[0] == "column" and b[2].casefold() == str(chosen["name"]).casefold()
            for b in on_visual
        )
        direction = "ascending" if is_column or re.search(r"alphabetic", f.low) else "descending"
    made = [{"op": "set_sort", "visual": entry.id, "field": chosen, "direction": direction}]
    return f.apply([entry], made, f"Sorted {entry.label} by {chosen['name']}, {direction}.")


# --- structure: delete, duplicate, visibility, z-order, type -----------------------------------


def delete_clause(f: Frame) -> Clause | None:
    if not re.search(
        r"\b(?:delete|remove|drop|erase|get\s+rid\s+of|take\s+out|trash|kill|ditch|scrap|zap|"
        r"toss|nuke|lose)\b",
        f.masked,
    ):
        return None
    if _PROPERTY_NOUNS.search(f.masked) or FIELD_OR_MODEL.search(f.masked):
        # a part of the visual, a field or the model: never the whole visual
        return None
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    made = [{"op": "delete_visual", "visual": e.id} for e in entries]
    on_page = [v for v in f.context.visuals if not v.is_group and v.page_id == entries[0].page_id]
    if len(entries) >= 4 and len(entries) >= 0.75 * len(on_page):
        # "delete everything": most of a page in one word is asked about first
        return f.clarify(
            f"That deletes {len(entries)} of the {len(on_page)} visuals on the page. Do you want "
            "all of them gone?",
            [
                Choice(
                    label=f"Yes, delete {len(entries)} visuals",
                    ops=tuple(made),
                    summary=f"Deleted {_phrase(entries)}.",
                )
            ],
            entries,
        )
    return f.apply(entries, made, f"Deleted {_phrase(entries)}.")


_COUNT_WORDS = {
    "once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}  # fmt: skip
_COPIES = re.compile(
    r"\b(?P<n>\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten)\s+"
    r"(?:more\s+|extra\s+|new\s+)?(?:times|copies|copy|duplicates?)\b|\b(?P<w>once|twice|thrice)\b",
    re.I,
)
MAX_COPIES = 10


def _copies(m: str) -> int | None:
    """How many copies a request asks for: "3 times", "twice", "make 3 copies"; 1 when it
    says nothing, None when it asks for none or too many."""
    found = _COPIES.search(m)
    if found is None:
        return 1
    word = (found.group("n") or found.group("w")).casefold()
    count = int(word) if word.isdigit() else _COUNT_WORDS[word]
    return count if 1 <= count <= MAX_COPIES else None


def duplicate_clause(f: Frame) -> Clause | None:
    if not re.search(
        r"\b(?:duplicate|clone|copy)\b|\bmake\s+(?:a\s+|\d+\s+|\w+\s+)?cop(?:y|ies)\b", f.masked
    ):
        return None
    if re.search(r"\b(?:copy|clone|duplicate)\b.{0,15}\b(?:format|formatting|style)\b", f.masked):
        return None
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    count = _copies(f.masked)
    if count is None:
        return f.clarify(
            f"I can make between 1 and {MAX_COPIES} copies at a time. How many copies of "
            f"{_phrase(entries)} do you want?"
        )
    destination = _destination_page(f)
    made: list[Op] = []
    for entry in entries:
        for _ in range(count):
            op: Op = {"op": "duplicate_visual", "visual": entry.id}
            if destination is not None and destination.id != entry.page_id:
                op["page"] = destination.id
            made.append(op)
    where = (
        f" onto page '{destination.name}'"
        if destination and destination.id != entries[0].page_id
        else ""
    )
    times = f" {count} times" if count > 1 else ""
    return f.apply(entries, made, f"Duplicated {_phrase(entries)}{times}{where}.")


def _destination_page(f: Frame) -> PageEntry | None:
    """The page a duplicate goes to: "duplicate the card onto page 2"."""
    match = re.search(
        r"\b(?:to|onto|into|on)\s+(?:the\s+)?(?:[\w'-]+\s+){0,2}?page\b.*$|\b(?:to|onto|into)\s+(?:page\b.*)$",
        f.low,
    )
    if not match:
        return None
    page, _ = find_page(match.group(0), f.context)
    return page


_HIDE = re.compile(
    r"\b(?:hide|unhide|invisible|visible|hidden|show\s+again|reveal)\b|\bmake\s+\w+\s+(?:visible|hidden|invisible)\b",
    re.I,
)


def visibility_clause(f: Frame) -> Clause | None:
    m = f.masked
    if not _HIDE.search(m) and not re.search(r"\bshow\b", m):
        return None
    if _PROPERTY_NOUNS.search(m) or FIELD_OR_MODEL.search(m):
        return None
    if label_units(m) is not None or label_decimals(m) is not None:
        return None  # "show values in thousands" changes their number format
    hide = bool(re.search(r"\b(?:hide|invisible|hidden)\b", m)) and not re.search(
        r"\bunhide\b|\bshow\b|\bvisible\b|\breveal\b", m
    )
    show_words = bool(re.search(r"\b(?:unhide|show|visible|reveal)\b", m))
    if not hide and not show_words:
        return None
    if show_words and not hide:
        # "show" alone reads as a data request unless it names a visual that is hidden
        hidden_pool = resolve_visuals(
            f.text, f.context, carry=None, forced=f.forced.visuals, only_hidden=True
        )
        if hidden_pool.ok and hidden_pool.explicit:
            f.res = hidden_pool
        elif re.search(r"\b(?:unhide|reveal|again|hidden)\b", m):
            if not any(v.hidden and not v.is_group for v in f.context.visuals):
                return f.not_available("No visual in this report is hidden.")
            f.res = hidden_pool
        else:
            return None
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    made = [{"op": "set_visual_hidden", "visual": e.id, "hidden": hide} for e in entries]
    verb = "Hid" if hide else "Showed"
    return f.apply(entries, made, f"{verb} {_phrase(entries)}.")


_Z = re.compile(
    r"\b(?:bring|send|move|put|push)\b.{0,25}?\b(?P<to>(?:to\s+(?:the\s+)?)?(?:front|back|forward|backward|backwards|top\s+layer|bottom\s+layer)|"
    r"in\s+front(?:\s+of\s+everything)?|behind(?:\s+everything)?|"
    r"on\s+top\s+of\s+(?:everything|all(?:\s+the\s+others)?|the\s+others|other\s+visuals))\b|"
    r"\b(?P<solo>bring\s+forward|send\s+backward|bring\s+to\s+front|send\s+to\s+back)\b",
    re.I,
)


def zorder_clause(f: Frame) -> Clause | None:
    match = _Z.search(f.masked)
    if not match:
        return None
    word = (match.group("to") or match.group("solo") or "").casefold()
    if "forward" in word and "front" not in word:
        to = "forward"
    elif "backward" in word:
        to = "backward"
    elif "front" in word or "top" in word:
        to = "front"
    elif "back" in word or "behind" in word or "bottom" in word:
        to = "back"
    else:
        return None
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if len(entries) > 1:
        return f.clarify(
            "Which visual should move? " + names_phrase(entries),
            [Choice(label=e.label, fill={"visual": e.id}) for e in entries[:6]],
        )
    made = [{"op": "arrange", "visual": entries[0].id, "to": to}]
    place = {
        "front": "to the front",
        "back": "to the back",
        "forward": "forward",
        "backward": "backward",
    }[to]
    return f.apply(
        entries,
        made,
        f"Brought {entries[0].label} {place}.".replace(
            "Brought", "Sent" if to in {"back", "backward"} else "Brought"
        ),
    )


_TYPE_PHRASES = sorted(ops.ADD_TYPES, key=len, reverse=True)
_TYPE_ALT = "|".join(re.escape(t).replace(r"\ ", r"[\s-]+") for t in _TYPE_PHRASES)
_TYPE_RE = re.compile(rf"\b(?P<type>{_TYPE_ALT})(?:e?s)?\b", re.I)
_TYPE_ALIASES = {"graph": "chart", "plot": "chart", "bar graph": "bar chart"}


def normalise_type(text: str) -> str | None:
    """The key of ADD_TYPES a phrase names ("line graph" is "line chart"), or None."""
    words = re.sub(r"[\s-]+", " ", text.casefold()).strip()
    for alias, real in _TYPE_ALIASES.items():
        words = re.sub(rf"\b{alias}\b", real, words)
    words = re.sub(
        r"\b(charts|cards|tables|slicers|matrices|matrixes)\b",
        lambda m: m.group(1)[:-1] if m.group(1) not in {"matrices", "matrixes"} else "matrix",
        words,
    )
    return words if words in ops.ADD_TYPES else None


def find_new_type(text: str) -> tuple[str, str, int] | None:
    """(type key, visual type, start of the phrase) of the LAST type phrase in ``text`` that
    is introduced by "into", "to", "as", "a" or "an": the visual type being asked for."""
    found: tuple[str, str, int] | None = None
    graph = re.sub(r"\bgraph\b", "chart", text, flags=re.I)
    for match in _TYPE_RE.finditer(graph):
        before = graph[: match.start()].rstrip()
        if not re.search(r"\b(?:into|to|as|an?|type\s+to|type\s+of)$", before, re.I):
            continue
        key = normalise_type(match.group("type"))
        if key:
            found = (key, ops.ADD_TYPES[key], match.start())
    return found


def change_type_clause(f: Frame) -> Clause | None:
    if not re.search(r"\b(?:make|turn|change|convert|switch|transform|swap)\b", f.masked):
        return None
    new = find_new_type(f.masked)
    if new is None:
        return None
    key, visual_type, start = new
    if re.search(r"\b(?:add|insert|create|build)\b", f.masked):
        return None
    before = f.text[:start]
    res = resolve_visuals(before or f.text, f.context, carry=f.carry, forced=f.forced.visuals)
    f.res = res
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    entries = [e for e in entries if e.visual_type != visual_type]
    if not entries:
        return f.not_available(f"It already is a {key}.")
    made = [
        {"op": "change_visual_type", "visual": e.id, "visual_type": visual_type} for e in entries
    ]
    return f.apply(entries, made, f"Changed {_phrase(entries)} into a {key}.")


# --- layout --------------------------------------------------------------------------------------


_DIMS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pixels?)?\s*(?:x|×|by)\s*(\d+(?:\.\d+)?)\s*(?:px|pixels?)?", re.I
)
_WIDE = re.compile(r"\b(?:wider|widen|broader|stretch)\b", re.I)
_NARROW = re.compile(r"\b(?:narrower|narrow|thinner|slimmer)\b", re.I)
_TALL = re.compile(r"\b(?:taller|heighten|lengthen)\b", re.I)
_SHORT = re.compile(r"\b(?:shorter|shorten|squat)\b", re.I)


@dataclass
class _Resize:
    """What a resize request asks for, before any visual is looked at."""

    kind: str  # dims | full | same | scale | relative | unclear
    words: str = ""  # how the receipt says it ("wider", "to twice the size")
    width: float | None = None
    height: float | None = None
    fw: float = 1.0
    fh: float = 1.0
    dw: float = 0.0  # a change in pixels ("10 px wider")
    dh: float = 0.0


_WIDTH_OF = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pixels?)?\s*(?:wide|width)\b|"
    r"\bwidth\s*(?:to|of|at|=|:|is)?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_HEIGHT_OF = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pixels?)?\s*(?:tall|high|height)\b|"
    r"\bheight\s*(?:to|of|at|=|:|is)?\s*(\d+(?:\.\d+)?)",
    re.I,
)
_SAME_SIZE = re.compile(
    r"\b(?:same|equal|identical)\s+(?:size|width|height|dimensions)\s+(?:as|to)\b", re.I
)
_FULL_WIDTH = re.compile(
    r"\b(?:full|entire|whole|page)[\s-]*width\b|\bacross\s+the\s+(?:whole\s+)?page\b", re.I
)


def _resize_intent(m: str) -> _Resize | None:
    dims = _DIMS.search(m)
    if dims:
        w, h = float(dims.group(1)), float(dims.group(2))
        return _Resize("dims", f"to {plain_number(w)} x {plain_number(h)}", width=w, height=h)
    if _FULL_WIDTH.search(m):
        return _Resize("full", "to the full width of the page")
    if _SAME_SIZE.search(m) or re.search(r"\b(?:same|equal|identical|uniform)\s+size\b", m):
        return _Resize("same")
    width_of, height_of = _WIDTH_OF.search(m), _HEIGHT_OF.search(m)
    scale = re.search(r"\b(twice|double|2x|half|halve)\b", m)
    only_width = bool(re.search(r"\b(?:width|wide)\b", m)) and not re.search(
        r"\b(?:height|tall|high)\b", m
    )
    only_height = bool(re.search(r"\b(?:height|tall|high)\b", m)) and not re.search(
        r"\b(?:width|wide)\b", m
    )
    if scale and not (width_of or height_of):
        half = scale.group(1).casefold() in {"half", "halve"}
        k = 0.5 if half else 2.0
        what = "width" if only_width else "height" if only_height else "size"
        return _Resize(
            "scale",
            f"to {'half' if half else 'twice'} the {what}",
            fw=1.0 if only_height else k,
            fh=1.0 if only_width else k,
        )
    wide, narrow, tall, short = (
        bool(_WIDE.search(m)),
        bool(_NARROW.search(m)),
        bool(_TALL.search(m)),
        bool(_SHORT.search(m)),
    )
    grow = 1 if _GROW_UP.search(m) else -1 if _GROW_DOWN.search(m) else 0
    amounts = find_amounts(m)
    pixels = next((a for a in amounts if a.unit == "px"), None)
    percent = next((a for a in amounts if a.unit == "%"), None)
    up = 1.1 if _SLIGHT.search(m) else 1.6 if _MUCH.search(m) else 1.2
    down = 1 / up
    if percent is not None and not (
        re.match(r"\s*(?:wider|narrower|taller|shorter|bigger|smaller|larger)\b", m[percent.end :])
        or re.search(r"\bby\s*$", m[: percent.start])
    ):
        percent = None  # "100% transparent" is not a size
    if percent is not None:
        shrinking = narrow or short or grow < 0
        if percent.value <= 0 or (shrinking and percent.value >= 100):
            return _Resize("unclear")  # "-50% wider" and "100% narrower" say nothing usable
        up, down = 1 + percent.value / 100, 1 - percent.value / 100
    if (
        (wide or narrow or tall or short)
        and pixels is not None
        and re.search(r"\b(?:to|at)\s*$", m[: pixels.start])
    ):
        # "widen the chart to 500 pixels": the new size, not 500 more
        across = wide or narrow
        what = "width" if across else "height"
        return _Resize(
            "dims",
            f"to a {what} of {plain_number(pixels.value)}",
            width=pixels.value if across else None,
            height=None if across else pixels.value,
        )
    if (wide or narrow or tall or short) and pixels is not None:
        word = "wider" if wide else "narrower" if narrow else "taller" if tall else "shorter"
        change = pixels.value * (1 if (wide or tall) else -1)
        return _Resize(
            "relative",
            word,
            dw=change if (wide or narrow) else 0.0,
            dh=change if (tall or short) else 0.0,
        )
    if wide or narrow or tall or short:
        fw = up if wide else down if narrow else 1.0
        fh = up if tall else down if short else 1.0
        word = "wider" if wide else "narrower" if narrow else "taller" if tall else "shorter"
        return _Resize("relative", word, fw=fw, fh=fh)
    if width_of or height_of:
        new_width = float(next(g for g in width_of.groups() if g)) if width_of else None
        new_height = float(next(g for g in height_of.groups() if g)) if height_of else None
        words = " and ".join(
            p
            for p in (
                f"to a width of {plain_number(new_width)}" if new_width is not None else "",
                f"to a height of {plain_number(new_height)}" if new_height is not None else "",
            )
            if p
        )
        return _Resize("dims", words, width=new_width, height=new_height)
    if grow:
        k = up if grow > 0 else down
        return _Resize("relative", "bigger" if grow > 0 else "smaller", fw=k, fh=k)
    if re.search(r"\b(?:resize|enlarge|shrink|scale|stretch|expand|grow)\b", m):
        return _Resize("unclear")
    return None


def resize_clause(f: Frame) -> Clause | None:
    """Resize: "make it bigger", "a bit wider", "10 px taller", "500 wide", "300 x 200", "double the
    size", "the same size as the Country slicer", "full width"."""
    m = f.masked
    intent = _resize_intent(m)
    if intent is None:
        return None
    reference: VisualEntry | None = None
    subject_text = f.text
    if intent.kind == "same":
        marker = _SAME_SIZE.search(f.text)
        if marker:
            found = resolve_visuals(f.text[marker.end() :], f.context, carry=None, forced=None)
            if found.ok and len(found.entries) == 1:
                reference = found.entries[0]
                subject_text = f.text[: marker.start()]
    f.res = resolve_visuals(subject_text, f.context, carry=f.carry, forced=f.forced.visuals)
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    if intent.kind == "unclear":
        return f.clarify(
            f'Resize {_phrase(entries)} to what size? For example "make it 400 x 250" or '
            '"a bit wider".',
            [],
            entries,
        )
    if intent.kind == "same":
        if reference is None and len(entries) > 1:
            reference = entries[0]
        if reference is None:
            return f.clarify(
                'The same size as which visual? Name it, for example "the Country slicer".'
            )
    made: list[Op] = []
    for entry in entries:
        page = _page_of(f, entry)
        try:
            made += _resize_ops(entry, page, intent, reference)
        except ops.Unfit:
            continue
    if not made:
        if reference is not None:
            return f.not_available(f"{_phrase(entries)} is already the size of {reference.label}.")
        return f.not_available(f"{_phrase(entries)} cannot change that way on this page.")
    described = intent.words
    if intent.kind == "same" and reference is not None:
        described = f"to the size of {reference.label}"
    note = ""
    if len(entries) == 1:
        first = entries[0]
        op = next((o for o in made if o["visual"] == first.id), {})
        width = plain_number(round(float(op.get("width", first.width)), 1))
        height = plain_number(round(float(op.get("height", first.height)), 1))
        note = f" (now {width} x {height})"
    verb = "Resized" if described.startswith("to ") else "Made"
    return f.apply(entries, made, f"{verb} {_phrase(entries)} {described}{note}.", how=f.res.how)


def _resize_ops(
    entry: VisualEntry, page: PageEntry, intent: _Resize, reference: VisualEntry | None
) -> list[Op]:
    if intent.kind == "dims":
        return ops.rect_ops(entry, page, width=intent.width, height=intent.height)
    if intent.kind == "full":
        margin = ops.gap(page)
        return ops.rect_ops(entry, page, x=margin, width=page.width - 2 * margin)
    if intent.kind == "same" and reference is not None:
        return ops.rect_ops(entry, page, width=reference.width, height=reference.height)
    if intent.dw or intent.dh:
        return ops.rect_ops(
            entry, page, width=entry.width + intent.dw, height=entry.height + intent.dh
        )
    return ops.scale_ops(entry, page, intent.fw, intent.fh)


_MOVE = re.compile(
    r"\b(?:move|shift|nudge|push|slide|drag|scoot|relocate|reposition|put|place|send|bring|"
    r"position|lower|raise|lift)\b",
    re.I,
)
_DIRECTION = re.compile(
    r"\b(left|right|up|down|higher|lower|raise|lift|upward|downward|upwards|downwards|top|"
    r"bottom)\b",
    re.I,
)


_DIRECTION_WORDS = {
    "left": "left",
    "right": "right",
    "up": "up",
    "upward": "up",
    "upwards": "up",
    "higher": "up",
    "raise": "up",
    "lift": "up",
    "down": "down",
    "downward": "down",
    "downwards": "down",
    "lower": "down",
    "top": "top",
    "bottom": "bottom",
}
_EDGE_WORDS = re.compile(
    r"\b(?:edge|side|corner|of\s+the\s+(?:page|screen|report|dashboard)|all\s+the\s+way|far)\b"
)


def move_clause(f: Frame) -> Clause | None:
    """Move: "move it left", "nudge it up 20 px", "move the table to the top", "put it at x 100"."""
    m = f.masked
    if not _MOVE.search(m):
        return None
    if re.search(r"\b(?:bring|pull)\s+up\b", m) and not re.search(r"\b(?:it|them)\s+up\b", m):
        return None  # "bring up the sales by store" asks to see them, not to move anything
    directions = list(dict.fromkeys(_DIRECTION_WORDS[d.casefold()] for d in _DIRECTION.findall(m)))
    at_x = re.search(r"\bx\s*[=:]?\s*(-?\d+(?:\.\d+)?)", m)
    at_y = re.search(r"\by\s*[=:]?\s*(-?\d+(?:\.\d+)?)", m)
    exact = at_x or at_y
    if not directions and not exact:
        return None
    entries = _need_entries(f)
    if isinstance(entries, Clause):
        return entries
    edge = bool(_EDGE_WORDS.search(m))
    amounts = [a for a in find_amounts(m) if a.unit in {"", "px", "%"}]
    scale = 0.5 if _SLIGHT.search(m) else 3.0 if _MUCH.search(m) else 1.0
    made: list[Op] = []
    for entry in entries:
        page = _page_of(f, entry)
        x = y = None
        if exact:
            # "at x 1300, y 40": both of them
            x = float(at_x.group(1)) if at_x else None
            y = float(at_y.group(1)) if at_y else None
        corner = any(d in {"top", "bottom"} for d in directions)
        for direction in directions:
            horizontal = direction in {"left", "right"}
            step = ops.nudge_step(page, horizontal, scale)
            if amounts:
                amount = amounts[0]
                span = page.width if horizontal else page.height
                step = amount.value / 100 * span if amount.unit == "%" else amount.value
            # "put it at the top left" is a corner, so left and right mean the edge there
            to_edge = edge or corner
            if direction == "left":
                x = 0.0 if to_edge else entry.x - step
            elif direction == "right":
                x = page.width - entry.width if to_edge else entry.x + step
            elif direction == "top":
                y = 0.0
            elif direction == "bottom":
                y = page.height - entry.height
            elif direction == "up":
                y = entry.y - step
            else:
                y = entry.y + step
        try:
            made += ops.rect_ops(entry, page, x=x, y=y)
        except ops.Unfit:
            continue
    if not made:
        return f.not_available(f"{_phrase(entries)} cannot move any further that way.")
    return f.apply(
        entries,
        made,
        f"Moved {_phrase(entries)} {_move_words(directions, amounts, edge)}.",
        how=f.res.how,
    )


def _move_words(directions: list[str], amounts: list[Amount], edge: bool) -> str:
    """A move in words: "50 px to the right", "10% up", "to the top of the page"."""
    if not directions:
        return "to that spot"
    vertical = [d for d in directions if d in {"top", "bottom"}]
    sideways = [d for d in directions if d in {"left", "right"}]
    if vertical and sideways:
        return f"to the {vertical[0]} {sideways[0]} corner of the page"
    where = {
        "left": "to the left",
        "right": "to the right",
        "up": "up",
        "down": "down",
        "top": "to the top of the page",
        "bottom": "to the bottom of the page",
    }
    words = " and ".join(where[d] for d in directions)
    if edge and set(directions) <= {"left", "right"}:
        words = " and ".join(f"to the {d} edge of the page" for d in directions)
    if amounts and not edge and not set(directions) & {"top", "bottom"}:
        unit = amounts[0].unit
        amount = plain_number(amounts[0].value) + ("%" if unit == "%" else " px")
        return f"{amount} {words}"
    return words


# --- styling: read, then refused --------------------------------------------------------------


@dataclass
class Action:
    part: str
    attrs: dict[str, Any] = field(default_factory=dict)


def _color_target(parts: list[Part], color: ColorMatch) -> Part | None:
    """The part a color belongs to: the one it directly follows or precedes."""
    named = [p for p in parts if p.name in COLOR_PARTS]
    if not named:
        return None
    following = [p for p in named if 0 <= p.start - color.end <= 24]
    preceding = [p for p in named if 0 <= color.start - p.end <= 24]
    if following:
        return min(following, key=lambda p: p.start - color.end)
    if preceding:
        return min(preceding, key=lambda p: color.start - p.end)
    return min(named, key=lambda p: min(abs(p.start - color.end), abs(color.start - p.end)))


_RADIUS = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pixels?|pt)?\s*(?:rounded\s+)?(?:radius|corners?)|"
    r"(?:radius|corners?)\s*(?:of|to|=|:|at)?\s*(\d+(?:\.\d+)?)"
)
_SQUARE = re.compile(r"\b(?:square|sharp|no\s+round\w*|not\s+round\w*|straight)\b")
_BORDER_WIDTH = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:px|pt|pixels?)?\s*(?:wide\s+)?border|"
    r"width\s*(?:of|to|=|:)?\s*(\d+(?:\.\d+)?)"
)
_AXIS_START = re.compile(
    r"\b(?:start|begin|min(?:imum)?)\w*\s+(?:at\s+|from\s+|to\s+)?(-?\d+(?:\.\d+)?)"
)
_AXIS_END = re.compile(r"\b(?:end|max(?:imum)?|stop)\w*\s+(?:at\s+|to\s+)?(-?\d+(?:\.\d+)?)")


class _Actions:
    """The attributes a clause gives each text part or box setting of a visual."""

    def __init__(self) -> None:
        self.by_part: dict[str, Action] = {}

    def __call__(self, part: str) -> dict[str, Any]:
        return self.by_part.setdefault(part, Action(part)).attrs


def _assign_colors(act: _Actions, s: Slots, forced_part: str | None) -> None:
    """A color goes to the part it sits next to; with no part named, to the default one."""

    def part_for(name: str) -> str:
        if name in {"__color__", "color_generic", "text"} and forced_part:
            return forced_part
        return {"color_generic": "__color__", "text": "__text__"}.get(name, name)

    for color in s.colors:
        target = _color_target(s.parts, color)
        attrs = act(part_for(target.name if target else "__color__"))
        attrs["color"] = color.hex
        attrs["color_word"] = color.text.casefold()
    if s.shade is not None and not s.colors:
        named = [p for p in s.parts if p.name in COLOR_PARTS]
        attrs = act(part_for(named[0].name if named else "__color__"))
        attrs["shade"] = s.shade
        attrs["color_word"] = "darker" if s.shade < 0 else "lighter"


def _assign_positions(act: _Actions, s: Slots, low: str) -> None:
    """Where the legend, data labels or title text sits: "at the bottom", "outside end"."""
    legend = next((p for p in s.parts if p.name == "legend"), None)
    labels = next((p for p in s.parts if p.name == "data_labels"), None)
    title = next((p for p in s.parts if p.name == "title"), None)
    if legend is not None:
        after = low[legend.end :] or low
        position = _phrase_position(after, _LEGEND_POSITIONS) or _phrase_position(
            low, _LEGEND_POSITIONS
        )
        if position:
            act("legend")["position"] = position
    elif labels is not None:
        position = _phrase_position(low, _LABEL_POSITIONS)
        if position:
            act("data_labels")["position"] = position
    if title is not None and legend is None and labels is None:
        alignment = title_alignment(low)
        if alignment:
            act("title")["alignment"] = alignment


def _assign_font(act: _Actions, s: Slots, forced_part: str | None) -> bool:
    """Size, bold, italic and family go to the text part named, else to the default part.
    Returns True when a size word names no text part and so means the visual's own size."""
    if not (
        s.size is not None
        or s.delta is not None
        or s.grow
        or s.bold is not None
        or s.italic is not None
        or s.family
    ):
        return False
    text_parts = [p for p in s.parts if p.name in TEXT_PARTS]
    # "the font" names no part: a family then applies to every text part of the visual
    specific = [p for p in text_parts if p.name != "text"]
    generic_text = any(p.name == "text" for p in text_parts)
    target = specific[0].name if specific else None
    if target is None and forced_part and forced_part in TEXT_PARTS:
        target = forced_part
    leftover_grow = False
    if s.size is not None:
        act(target or "__text__")["size"] = s.size
    elif s.delta is not None:
        act(target or "__text__")["delta"] = s.delta
    elif s.grow:
        if target or generic_text:
            act(target or "__text__")["delta"] = 2.0 * s.degree * (1 if s.grow > 0 else -1)
        else:
            leftover_grow = True
    if s.bold is not None:
        act(target or "__text__")["bold"] = s.bold
    if s.italic is not None:
        act(target or "__text__")["italic"] = s.italic
    if s.family:
        act(target or "__family__")["family"] = s.family
    return leftover_grow


def _assign_box(act: _Actions, s: Slots, low: str) -> None:
    """Corners, transparency and border width."""
    for part in s.parts:
        if part.name == "corners":
            radius = _RADIUS.search(low)
            if _SQUARE.search(low) or s.toggle is False:
                act("corners")["radius"] = 0.0
            elif radius:
                act("corners")["radius"] = float(radius.group(1) or radius.group(2))
            else:
                act("corners")["radius"] = 8.0
        elif part.name == "transparency":
            percent = next((a for a in find_amounts(low) if a.unit == "%"), None)
            if s.toggle is False:
                act("transparency")["value"] = 0.0
            else:
                act("transparency")["value"] = percent.value if percent else 100.0
        elif part.name == "border":
            number = _BORDER_WIDTH.search(low)
            thickness = re.search(r"\b(?:thick(?:er)?|thin(?:ner)?|wider|bold(?:er)?)\b", low)
            if number:
                act("border")["width"] = float(number.group(1) or number.group(2))
            elif thickness:
                act("border")["thicker"] = "thin" not in thickness.group(0)


def build_actions(f: Frame, s: Slots) -> tuple[list[Action], bool]:
    """(the attributes per part, whether a size word is left for the visual's own size)."""
    act = _Actions()
    low = f.masked
    _assign_colors(act, s, None)
    if s.toggle is not None:
        for part in s.parts:
            if part.name in TOGGLE_PARTS:
                act(part.name)["show"] = s.toggle
    for part in s.parts:
        if part.name == "axis_title":
            # "the title of the x axis" is one axis's title, not both
            said = low[part.start : part.end]
            if re.search(r"\b(?:x|horizontal|category)\b", said):
                act("axis_title")["axes"] = ["x"]
            elif re.search(r"\b(?:y|vertical|value)\b", said):
                act("axis_title")["axes"] = ["y"]
    _assign_positions(act, s, low)
    if s.units:
        act("data_labels")["units"] = s.units
    if s.decimals is not None:
        act("data_labels")["decimals"] = s.decimals
    leftover_grow = _assign_font(act, s, None)
    _assign_box(act, s, low)
    start, end = _AXIS_START.search(low), _AXIS_END.search(low)
    if start or end:
        axis = "x_axis" if any(p.name == "x_axis" for p in s.parts) else "y_axis"
        if start:
            act(axis)["start"] = float(start.group(1))
        if end:
            act(axis)["end"] = float(end.group(1))
    return list(act.by_part.values()), leftover_grow


def _bg_words(low: str) -> bool:
    return bool(re.search(r"\b(?:no|remove|without|hide)\s+(?:the\s+)?background\b", low))


def styling_clause(f: Frame) -> Clause | None:
    """A request to change how a visual looks: its colors, fonts, title or caption, data
    labels, legend, axes, gridlines, background, border, shadow or corners. Studio does not
    change styling, so the answer is the refusal every styling request gets
    (``edit_capabilities.STYLING_REASON``); no target is looked for and no operation is built.

    Size words that name no text part ("make it bigger", "make the bars wider") are the visual's
    own size (``resize_clause``), and display units or decimals are a number format (the
    number-format card's, set_number_format): neither is refused here."""
    s = read_slots(f)
    if s.units or s.decimals is not None:
        return None
    if title_text(f) is not None:
        # a new title or caption ("rename the chart to Sales", "call this chart X")
        return f.unsupported(capability("styling"))
    return f.unsupported(capability("styling")) if styling_signal(f, s) else None


# Where an axis starts or ends, said anywhere in the clause ("end the y axis of X at 50000").
_AXIS_RANGE = re.compile(r"\b(?:start|begin|end|stop|min(?:imum)?|max(?:imum)?|range)\w*\b")
# Parts of a visual that only its formatting has: a request that names one and that nothing
# else read ("horizontal grid lines on the chart") is about how the visual looks.
_STYLE_PARTS = frozenset(
    {
        "legend",
        "data_labels",
        "gridlines",
        "axis_title",
        "title_background",
        "border",
        "shadow",
        "corners",
        "transparency",
    }
)


def styling_signal(f: Frame, s: Slots | None = None, *, parts_alone: bool = False) -> bool:
    """Whether the clause gives a part of a visual a look (a color, a size, a font, on or off,
    a place, an axis's range): ``build_actions`` finds something to change. With
    ``parts_alone`` a formatting-only part named with no value counts too (for words no
    recognizer took as an edit). A number format (units, decimals) never counts."""
    s = s or read_slots(f)
    if s.units or s.decimals is not None:
        return False
    if _bg_words(f.masked):
        s.parts = [p for p in s.parts if p.name != "background"] + [Part("background", 0, 0)]
    actions, _ = build_actions(f, s)
    if actions:
        return True
    if (
        any(p.name in {"x_axis", "y_axis", "axis"} for p in s.parts)
        and _AXIS_RANGE.search(f.masked) is not None
        and bool(find_amounts(f.masked))
    ):
        return True
    return parts_alone and any(p.name in _STYLE_PARTS for p in s.parts)


_TITLE_TO = re.compile(
    r"(?:\b(?:rename|retitle)\b.*?\b(?:to|as|:)\s+"
    r"|\b(?:call|name|title|caption|label)\s+(?!of\b)(?:it|them|(?:the\s+|this\s+|that\s+)?"
    r"(?:[\w'-]+\s+){0,3}?(?:charts?|graphs?|cards?|tables?|matri(?:x|ces)|slicers?|visuals?|"
    r"kpis?|gauges?|maps?|tiles?|bars?|columns?|pages?))\s+(?:to\s+|as\s+)?"
    r"|\b(?:titled|called|named)\s+"
    r"|\b(?:title|heading|caption|name|label)(?:\s+(?:text|name))?\s+(?:of\s+[^\"]+?\s+)?"
    r"(?:should\s+(?:say|read|be)\s+|says?\s+|reads?\s+|is\s+|=\s*|to\s+|as\s+|:\s*)"
    r"|\b(?:rename|retitle)\b[^\"'\x01]*?\s+(?=[\"'\x01])"
    r"|\b(?:title|heading|caption)\s+(?=[\"'\x01]))"
    r"(?P<value>.+)$",
    re.I,
)


def _title_value(text: str) -> tuple[str, tuple[int, int]] | None:
    """(the new text, where it sits in ``text``) of a rename-like request, quotes protected."""
    guarded, saved = mask_quotes(text)
    match = _TITLE_TO.search(guarded)
    if not match:
        return None
    value = unmask_quotes(match.group("value"), saved).strip().rstrip(".!?").strip()
    if not value:
        return None
    at = text.rfind(value)
    return value, (at, at + len(value)) if at >= 0 else (len(text), len(text))


def title_text(f: Frame) -> str | None:
    """New title text: quoted, or what follows "to" in a rename-like request."""
    low = f.low
    if not re.search(
        r"\b(?:rename|retitle|call|name|title|titled|caption|heading|label|named|called)\b", low
    ):
        return None
    if re.search(r"\bpages?\b", low) and not find_types(f.text):
        return None
    if re.search(
        r"\b(?:rename|retitle|call|name)\s+(?:the\s+|this\s+|that\s+)?(?:[\w'%/-]+\s+){0,3}?"
        r"(?:columns?(?!\s+(?:charts?|graphs?))|fields?|measures?)\b",
        low,
    ):
        return None  # a column's name or header ("rename the column StoreName"), not a title
    found = _title_value(f.text)
    if found is None:
        return None
    value = found[0]
    quoted = re.fullmatch(r"[\"'](.+)[\"']", value)
    if quoted:
        return quoted.group(1).strip()
    # "title of the first card should be bold": a predicate, not text; "label the bars of the
    # chart in millions": which bars, not text
    if re.match(r"(?:should|must|needs?|is|are|has|have|to\s+be|be|of)\b", value, re.I):
        return None
    # "add a title to the table": that names the visual, it is not the text
    if re.match(r"(?:the|this|that|these|those|all|every|each)\s", value, re.I) and find_types(
        value
    ):
        return None
    # A bare color or format word is not text: "change the title to blue"
    words = re.sub(
        r"\b(?:the|a|an|bold|italic|bigger|larger|smaller)\b", "", value, flags=re.I
    ).split()
    if find_colors(value) and len(words) <= 2:
        return None
    if re.fullmatch(
        r"(?:on|off|bigger|larger|smaller|bold|italic|visible|hidden|left|right|center|centre|"
        r"centered)",
        value,
        re.I,
    ):
        return None
    # "the title should be 14 pt": a size, not the words
    if re.fullmatch(
        r"[+-]?\d+(?:\.\d+)?\s*(?:pt|pts|points?|px|pixels?)?(?:\s+(?:big|high|tall))?",
        value.strip(),
        re.I,
    ):
        return None
    return value


def explicit_text_span(f: Frame) -> tuple[int, int] | None:
    """Where the new text sits in the clause, so the resolver does not read it as a name."""
    if not re.search(r"\b(?:rename|retitle|call|name|title|titled|caption|heading|label)\b", f.low):
        return None
    found = _title_value(f.text)
    return found[1] if found else None


def _blank_value(f: Frame) -> str:
    span = explicit_text_span(f)
    if not span:
        return f.masked
    chars = list(f.masked)
    for i in range(span[0], min(span[1], len(chars))):
        chars[i] = " "
    return "".join(chars)


def visual_clause(f: Frame) -> Clause | None:
    """Read a clause about visuals; None when it is none of the visual edits."""
    for recognizer in (
        fields_clause,
        sort_clause,
        change_type_clause,
        *LAYOUT_RECOGNIZERS,
        zorder_clause,
        duplicate_clause,
        visibility_clause,
        delete_clause,
        styling_clause,
        center_clause,
        move_clause,
        resize_clause,
    ):
        found = recognizer(f)
        if found is not None:
            return found
    return None


__all__ = ["Forced", "Frame", "find_parts", "visual_clause"]
