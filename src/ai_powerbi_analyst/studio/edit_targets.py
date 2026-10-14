"""Which visual, or visuals, does an edit request mean?

The resolver reads a clause for target cues and matches them against the request-scoped
index: the canvas selection, the caption a person sees, the fields shown, type words,
position words relative to same-type siblings ("the top left chart", "the second card"),
hue words ("the blue chart"), and plurals and quantifiers ("all the cards", "both
slicers"). It never guesses between candidates: when the cues fit several visuals it says
so, with their captions and positions, so the caller can ask.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .edit_context import (
    EditContext,
    PageEntry,
    VisualEntry,
    describe_position,
    type_name,
)
from .edit_text import LITERAL_MARK, tokens

Span = tuple[int, int]

_STOP = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "by", "with", "per", "vs", "at"}
)

CHART_TYPES = frozenset(
    {
        "columnChart",
        "clusteredColumnChart",
        "hundredPercentStackedColumnChart",
        "barChart",
        "clusteredBarChart",
        "hundredPercentStackedBarChart",
        "lineChart",
        "areaChart",
        "stackedAreaChart",
        "lineClusteredColumnComboChart",
        "lineStackedColumnComboChart",
        "pieChart",
        "donutChart",
        "treemap",
        "funnel",
        "waterfallChart",
        "ribbonChart",
        "scatterChart",
        "gauge",
    }
)
_COLUMN = frozenset({"columnChart", "clusteredColumnChart", "hundredPercentStackedColumnChart"})
_BAR = frozenset({"barChart", "clusteredBarChart", "hundredPercentStackedBarChart"})
HORIZONTAL_BARS = _BAR
CARD_TYPES = frozenset({"card", "cardVisual", "multiRowCard"})
SLICER_TYPES = frozenset({"slicer", "advancedSlicerVisual"})
TABLE_TYPES = frozenset({"tableEx", "table"})
_ANY: frozenset[str] = frozenset()  # every editable visual

# (pattern, groups, strong, plural-capable). Order matters: specific phrases first.
_TYPE_PATTERNS: tuple[tuple[re.Pattern[str], frozenset[str], bool], ...] = tuple(
    (re.compile(pattern, re.I), groups, strong)
    for pattern, groups, strong in (
        (r"\bcolumn\s*(?:chart|graph|plot)s?\b", _COLUMN, True),
        (r"\bbar\s*(?:chart|graph|plot)s?\b", _BAR, True),
        (r"\bline\s*(?:chart|graph|plot)s?\b", frozenset({"lineChart"}), True),
        (r"\barea\s*(?:chart|graph|plot)s?\b", frozenset({"areaChart", "stackedAreaChart"}), True),
        (
            r"\bcombo\s*(?:chart|graph|plot)?s?\b",
            frozenset({"lineClusteredColumnComboChart", "lineStackedColumnComboChart"}),
            True,
        ),
        (r"\bpie(?:\s*(?:chart|graph))?s?\b", frozenset({"pieChart"}), True),
        (r"\bdonut(?:\s*(?:chart|graph))?s?\b", frozenset({"donutChart"}), True),
        (r"\bscatter\w*(?:\s*(?:chart|graph|plot))?s?\b", frozenset({"scatterChart"}), True),
        (r"\btree\s*maps?\b", frozenset({"treemap"}), True),
        (r"\bwaterfall(?:\s*(?:chart|graph))?s?\b", frozenset({"waterfallChart"}), True),
        (r"\bfunnels?\b", frozenset({"funnel"}), True),
        (r"\bribbon\s*(?:chart|graph)s?\b", frozenset({"ribbonChart"}), True),
        (r"\bgauges?\b", frozenset({"gauge"}), True),
        (r"\bkpis?\b", frozenset({"kpi", *CARD_TYPES}), True),
        (r"\bmaps?\b", frozenset({"map", "filledMap", "azureMap"}), True),
        (r"\b(?:charts?|graphs?|plots?)\b", CHART_TYPES, True),
        (r"\b(?:cards?|tiles?|callouts?)\b", CARD_TYPES, True),
        (r"\b(?:slicers?|filter\s+boxes)\b", SLICER_TYPES, True),
        (r"\b(?:tables?|grids?)\b", TABLE_TYPES, True),
        (r"\b(?:matri(?:x|ces|xes)|pivot\s*tables?)\b", frozenset({"pivotTable"}), True),
        (r"\btext\s?box(?:es)?\b", frozenset({"textbox"}), True),
        (r"\b(?:images?|logos?|pictures?)\b", frozenset({"image"}), True),
        (r"\bbuttons?\b", frozenset({"actionButton", "pageNavigator", "bookmarkNavigator"}), True),
        (r"\bshapes?\b", frozenset({"shape"}), True),
        (r"\b(?:visuals?|visualizations?|objects?|elements?|things?|items?)\b", _ANY, True),
        # Weak: property words that also point at a chart type.
        (r"\bbars?\b", _COLUMN | _BAR, False),
        (r"\bcolumns\b", _COLUMN | _BAR, False),
        (r"\blines\b", frozenset({"lineChart", "lineClusteredColumnComboChart"}), False),
        (r"\bslices\b", frozenset({"pieChart", "donutChart"}), False),
    )
)
_PLURAL_WORD = re.compile(r"(?:s|es|ces)$|people$", re.I)
_QUANTIFIER = re.compile(r"\b(?:all|every|each|both|any|everything|every\s+single)\b", re.I)
_PRONOUN = re.compile(
    r"\b(?:this|it|its|them|their|these|those|selected|current(?:ly)?)\b|"
    r"\bthat\s+(?:one|visual|chart|card|slicer|table|matrix|graph)\b",
    re.I,
)

_POSITION_WORDS = (
    "top|bottom|upper|lower|left|right|middle|center|centre|central|first|second|third|"
    "fourth|fifth|sixth|last|leftmost|rightmost|topmost|bottommost|1st|2nd|3rd|4th|5th"
)
_HUES = (
    "red|orange|yellow|green|teal|blue|purple|pink|brown|gr[ae]y|black|white|navy|lime|"
    "turquoise|cyan|aqua|violet|magenta|maroon|crimson|gold"
)
_HUE_MAP = {
    "navy": "blue",
    "turquoise": "teal",
    "cyan": "teal",
    "aqua": "teal",
    "violet": "purple",
    "magenta": "pink",
    "maroon": "red",
    "crimson": "red",
    "gold": "yellow",
    "lime": "green",
    "grey": "gray",
}
_ORDINALS = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
    "sixth": 6,
    "last": -1,
}


@dataclass(frozen=True)
class TypeCue:
    groups: frozenset[str]
    strong: bool
    plural: bool
    word: str
    span: Span


@dataclass(frozen=True)
class PositionCue:
    vertical: str | None = None  # top | bottom | middle
    horizontal: str | None = None  # left | right | center
    ordinal: int | None = None  # 1-based, -1 for last

    def __bool__(self) -> bool:
        return bool(self.vertical or self.horizontal or self.ordinal)

    def phrase(self) -> str:
        if self.ordinal:
            return "last" if self.ordinal == -1 else f"number {self.ordinal}"
        return " ".join(p for p in (self.vertical, self.horizontal) if p)


@dataclass
class Resolution:
    entries: list[VisualEntry] = field(default_factory=list)
    how: str = ""  # selection | caption | field | position | type | hue | all | carry | forced
    candidates: list[VisualEntry] = field(default_factory=list)  # when ambiguous
    question: str = ""  # what to ask when ambiguous or nothing matched
    page: PageEntry | None = None  # a page the request named
    explicit: bool = False  # the request named its target, rather than relying on selection
    masked: str = ""  # the request with the words that named the target blanked out
    type_noun: str = ""  # "chart", "card"... the word to use when asking
    pronoun: bool = False  # the request said "this" or "it"
    spans: list[Span] = field(default_factory=list)
    pool: list[VisualEntry] = field(default_factory=list)  # the visuals that were searched

    @property
    def ok(self) -> bool:
        return bool(self.entries) and not self.candidates

    @property
    def ambiguous(self) -> bool:
        return bool(self.candidates)


# --- token matching --------------------------------------------------------------------------


def _message_tokens(text: str) -> list[tuple[str, int, int]]:
    """(sub-token, start, end) of every word in the request; CamelCase words split."""
    found: list[tuple[str, int, int]] = []
    for match in re.finditer(r"[A-Za-z0-9]+", text):
        for part in tokens(match.group(0)):
            found.append((part, match.start(), match.end()))
    return found


def _longest_run(message: list[str], wanted: list[str]) -> tuple[int, int]:
    """(length, start index in message) of the longest contiguous run shared by both."""
    best, at = 0, -1
    previous = [0] * (len(wanted) + 1)
    for i, word in enumerate(message, 1):
        current = [0] * (len(wanted) + 1)
        for j, other in enumerate(wanted, 1):
            if word == other:
                current[j] = previous[j - 1] + 1
                if current[j] > best:
                    best, at = current[j], i - current[j]
        previous = current
    return best, at


def _meaningful(words: Iterable[str]) -> bool:
    return any(w not in _STOP for w in words)


def _substantial(words: Iterable[str]) -> bool:
    """Whether a partial match rests on a real word: "100K" splits into "100" and "k", which is
    a number, not the name of a visual."""
    return any(w not in _STOP and not w.isdigit() and len(w) > 1 for w in words)


@dataclass
class NameMatch:
    entry: VisualEntry
    tier: int  # 1 caption in full, 2 field in full, 3 caption in part, 4 field in part
    run: int
    span: Span


def match_names(
    text: str, entries: list[VisualEntry], exclude: list[Span] | None = None
) -> list[NameMatch]:
    """Visuals whose caption or fields the request quotes, best matches first."""
    words = [w for w in _message_tokens(text) if not any(s <= w[1] < e for s, e in (exclude or []))]
    plain = [w[0] for w in words]
    found: list[NameMatch] = []
    for entry in entries:
        best: NameMatch | None = None
        candidates = [(1, entry.caption)] if entry.caption else []
        candidates += [(2, name) for name in entry.fields]
        for tier, name in candidates:
            wanted = tokens(name)
            if not wanted or not _meaningful(wanted):
                continue
            run, at = _longest_run(plain, wanted)
            if run == 0:
                continue
            full = run == len(wanted)
            if not full and (run < 2 or not _substantial(plain[at : at + run])):
                continue
            level = tier if full else tier + 2
            span = (words[at][1], words[at + run - 1][2])
            match = NameMatch(entry, level, run, span)
            if best is None or (level, -run) < (best.tier, -best.run):
                best = match
        if best is not None:
            found.append(best)
    return sorted(found, key=lambda m: (m.tier, -m.run))


# --- cues ----------------------------------------------------------------------------------


def find_types(text: str) -> list[TypeCue]:
    """The visual-type words in a request, each claiming its span once."""
    found: list[TypeCue] = []
    taken: list[Span] = []
    for pattern, groups, strong in _TYPE_PATTERNS:
        for match in pattern.finditer(text):
            span = (match.start(), match.end())
            if any(span[0] < e and s < span[1] for s, e in taken):
                continue
            word = match.group(0).casefold()
            plural = bool(_PLURAL_WORD.search(word)) and not word.endswith(
                ("glass", "gauss", "ss", "pass")
            )
            if groups is _ANY and word in {"things", "items", "elements", "objects", "visuals"}:
                plural = True
            found.append(TypeCue(groups, strong, plural, word, span))
            taken.append(span)
    return sorted(found, key=lambda c: c.span)


_POSITION_BEFORE = re.compile(
    rf"(?P<words>(?:(?:{_POSITION_WORDS})(?:[\s-]+(?:hand|most))?[\s,-]*(?:and\s+)?)+)$", re.I
)
_POSITION_AFTER = re.compile(
    rf"^\s*(?:(?:on|at|in|from)\s+(?:the\s+)?)(?P<words>(?:(?:{_POSITION_WORDS})[\s-]*)+)"
    r"(?:\s*(?:corner|side|part|half|row|column|edge|of\s+the\s+(?:page|screen|report|dashboard)))?",
    re.I,
)


def _position_from(words: str) -> PositionCue:
    vertical = horizontal = None
    ordinal = None
    for word in re.findall(r"[a-z0-9]+", words.casefold()):
        if word in {"top", "upper", "topmost"}:
            vertical = "top"
        elif word in {"bottom", "lower", "bottommost"}:
            vertical = "bottom"
        elif word in {"left", "leftmost"}:
            horizontal = "left"
        elif word in {"right", "rightmost"}:
            horizontal = "right"
        elif word in {"middle", "center", "centre", "central"}:
            vertical = vertical or "middle"
        elif word in _ORDINALS:
            ordinal = _ORDINALS[word]
    return PositionCue(vertical, horizontal, ordinal)


_QUANTITY = r"(?:(?:two|three|four|five|six|2|3|4|5|6)\s+)?"


def find_position(
    text: str, cue: TypeCue | None, *, after: bool = True
) -> tuple[PositionCue, Span | None]:
    """Position words in the noun phrase around a type word: "the top left chart", "the two
    top charts", "the chart on the right". Direction words after a verb ("move the table
    left") are not, and neither is a place a visual is sent to ("put the slicers on the left"):
    with a move verb ``after`` is False."""
    if cue is None:
        return PositionCue(), None
    before = text[: cue.span[0]]
    # Skip a hue word between the position words and the noun: "the top blue chart".
    before = re.sub(rf"\b(?:{_HUES})\s*$", "", before, flags=re.I)
    match = _POSITION_BEFORE.search(before.rstrip(" ,"))
    if match and re.search(
        rf"(?:^|\b)(?:the|this|that|a|an|all|both|of|on|to)\s+{_QUANTITY}$",
        before[: match.start()],
        re.I,
    ):
        cue_before = _position_from(match.group("words"))
        span = (match.start(), cue.span[0])
        tail = _POSITION_AFTER.match(text[cue.span[1] :]) if after and cue_before.ordinal else None
        if tail is not None and not (cue_before.horizontal or cue_before.vertical):
            # "the second chart from the left": the ordinal counts along that edge
            direction = _position_from(tail.group("words"))
            cue_before = PositionCue(direction.vertical, direction.horizontal, cue_before.ordinal)
            span = (span[0], cue.span[1] + tail.end("words"))
        return cue_before, span
    found = _POSITION_AFTER.match(text[cue.span[1] :]) if after else None
    if found:
        return _position_from(found.group("words")), (
            cue.span[1] + found.start("words"),
            cue.span[1] + found.end(),
        )
    return PositionCue(), None


def find_hue(text: str, cue: TypeCue | None) -> tuple[str | None, Span | None]:
    """A hue word right before a type word: "the blue chart"."""
    if cue is None:
        return None, None
    before = text[: cue.span[0]]
    match = re.search(
        rf"\b(?:the|all|those|these|every|both|that|this|a|an)\s+(?:(?:{_POSITION_WORDS})\s+)*(?P<hue>{_HUES})\s*$",
        before,
        re.I,
    )
    if not match:
        return None, None
    word = match.group("hue").casefold()
    return _HUE_MAP.get(word, word), (match.start("hue"), match.end("hue"))


def find_page(text: str, context: EditContext) -> tuple[PageEntry | None, list[Span]]:
    """The page a request names: "page 2", "the second page", "the Details page", "this page"."""
    pages = context.pages
    # ("the page 16:9" is a shape, not page 16)
    match = re.search(r"\bpage\s+(?:number\s+)?(\d{1,3})\b(?!\s*:\s*\d)", text, re.I)
    if match:
        number = int(match.group(1))
        by_name = next((p for p in pages if p.name.strip() == match.group(1)), None)
        if by_name:
            return by_name, [match.span()]
        if 1 <= number <= len(pages):
            return pages[number - 1], [match.span()]
    match = re.search(
        r"\b(first|second|third|fourth|fifth|last|next|previous)\s+page\b", text, re.I
    )
    if match:
        word = match.group(1).casefold()
        current = _active_index(context)
        index = {"next": current + 1, "previous": current - 1}.get(word)
        if index is None:
            ordinal = _ORDINALS[word]
            index = len(pages) - 1 if ordinal == -1 else ordinal - 1
        if 0 <= index < len(pages):
            return pages[index], [match.span()]
    for page in pages:
        name = page.name.strip()
        if len(name) < 2 or name.isdigit():
            continue
        wanted = tokens(name)
        if not wanted or not _meaningful(wanted):
            continue
        words = _message_tokens(text)
        run, at = _longest_run([w[0] for w in words], wanted)
        if run == len(wanted):
            return page, [(words[at][1], words[at + run - 1][2])]
    match = re.search(r"\b(?:this|the\s+current|the\s+active|current|active)\s+page\b", text, re.I)
    if match:
        active = _active_page(context)
        if active:
            return active, [match.span()]
    return None, []


def find_pages_by_name(text: str, context: EditContext) -> list[PageEntry]:
    """Every page whose whole name the request says ("the Forecast page and the Product Mix
    page"), in the order the report has them. ``find_page`` returns one of them only."""
    found: list[PageEntry] = []
    words = _message_tokens(text)
    for page in context.pages:
        name = page.name.strip()
        if len(name) < 2 or name.isdigit():
            continue
        wanted = tokens(name)
        if not wanted or not _meaningful(wanted):
            continue
        run, _ = _longest_run([w[0] for w in words], wanted)
        if run == len(wanted):
            found.append(page)
    # ("the Sales by Region page" also contains the page called Sales: the longer name wins)
    return [
        p
        for p in found
        if not any(
            q is not p
            and len(tokens(q.name)) > len(tokens(p.name))
            and _longest_run(tokens(q.name), tokens(p.name))[0] == len(tokens(p.name))
            for q in found
        )
    ]


def _active_index(context: EditContext) -> int:
    for index, page in enumerate(context.pages):
        if page.id == context.active_page:
            return index
    return 0


def _active_page(context: EditContext) -> PageEntry | None:
    if context.active_page:
        return context.page(context.active_page)
    if len(context.pages) == 1:
        return context.pages[0]
    return None


# --- geometry -------------------------------------------------------------------------------


def reading_order(entries: list[VisualEntry]) -> list[VisualEntry]:
    """Rows top to bottom (visuals whose centers share a row band), each row left to right."""
    rows: list[list[VisualEntry]] = []
    for entry in sorted(entries, key=lambda e: (e.cy, e.cx)):
        for row in rows:
            anchor = row[0]
            if abs(entry.cy - anchor.cy) <= 0.5 * min(entry.height, anchor.height):
                row.append(entry)
                break
        else:
            rows.append([entry])
    return [e for row in rows for e in sorted(row, key=lambda e: e.cx)]


def _band(entries: list[VisualEntry], axis: str, low: bool) -> list[VisualEntry]:
    """The entries at the top/bottom (axis "y") or left/right (axis "x") extreme, allowing for
    visuals of a row whose centers differ by a few pixels."""
    if not entries:
        return []
    if axis == "y":
        pick = min(entries, key=lambda e: e.cy) if low else max(entries, key=lambda e: e.cy)
        tolerance = 0.35 * min(e.height for e in entries)
        return [e for e in entries if abs(e.cy - pick.cy) <= tolerance]
    pick = min(entries, key=lambda e: e.cx) if low else max(entries, key=lambda e: e.cx)
    tolerance = 0.35 * min(e.width for e in entries)
    return [e for e in entries if abs(e.cx - pick.cx) <= tolerance]


def apply_position(entries: list[VisualEntry], cue: PositionCue) -> list[VisualEntry]:
    """The entries that a position phrase picks among same-type siblings."""
    if cue.ordinal and (cue.horizontal or cue.vertical) and entries:
        # "the second chart from the left": rank along that edge; visuals in the same column
        # share a rank, and a rank of two is a question rather than a guess
        across = bool(cue.horizontal)
        reverse = cue.horizontal == "right" or cue.vertical == "bottom"

        def key(e: VisualEntry) -> float:
            return e.cx if across else e.cy

        size = min(e.width if across else e.height for e in entries)
        ranks: list[list[VisualEntry]] = []
        for entry in sorted(entries, key=key, reverse=reverse):
            if ranks and abs(key(ranks[-1][0]) - key(entry)) <= 0.35 * size:
                ranks[-1].append(entry)
            else:
                ranks.append([entry])
        index = len(ranks) - 1 if cue.ordinal == -1 else cue.ordinal - 1
        return ranks[index] if 0 <= index < len(ranks) else []
    if cue.ordinal:
        ordered = reading_order(entries)
        index = len(ordered) - 1 if cue.ordinal == -1 else cue.ordinal - 1
        return [ordered[index]] if 0 <= index < len(ordered) else []
    picked = entries
    if cue.vertical == "top":
        picked = _band(picked, "y", True)
    elif cue.vertical == "bottom":
        picked = _band(picked, "y", False)
    if cue.horizontal == "left":
        picked = _band(picked, "x", True)
    elif cue.horizontal == "right":
        picked = _band(picked, "x", False)
    if cue.vertical == "middle" or cue.horizontal == "center":
        mid_x = sum(e.cx for e in picked) / len(picked)
        mid_y = sum(e.cy for e in picked) / len(picked)
        picked = [min(picked, key=lambda e: (e.cx - mid_x) ** 2 + (e.cy - mid_y) ** 2)]
    return picked


def apply_region(
    entries: list[VisualEntry], cue: PositionCue, width: float, height: float
) -> list[VisualEntry]:
    """All the entries in the part of the page a position phrase names: "the slicers on the top
    right" are the slicers whose centers lie in the top right quarter, not one extreme."""
    picked = entries
    if cue.vertical == "top":
        picked = [e for e in picked if e.cy < height / 2]
    elif cue.vertical == "bottom":
        picked = [e for e in picked if e.cy >= height / 2]
    elif cue.vertical == "middle":
        picked = [e for e in picked if height / 3 <= e.cy <= 2 * height / 3]
    if cue.horizontal == "left":
        picked = [e for e in picked if e.cx < width / 2]
    elif cue.horizontal == "right":
        picked = [e for e in picked if e.cx >= width / 2]
    elif cue.horizontal == "center":
        picked = [e for e in picked if width / 3 <= e.cx <= 2 * width / 3]
    return picked


# --- the resolver --------------------------------------------------------------------------


def _names(entries: list[VisualEntry], limit: int = 6) -> str:
    labels = [e.label for e in entries[:limit]]
    extra = len(entries) - limit
    if extra > 0:
        labels.append(f"{extra} more")
    if len(labels) == 1:
        return labels[0]
    return ", ".join(labels[:-1]) + " or " + labels[-1]


def relative_positions(group: list[VisualEntry]) -> dict[str, str]:
    """Where each of several visuals sits among the others: "top left", "second from left"...

    Words that only say where a visual is on the page ("left") would repeat for a group that
    fills one half of it, so the words compare the group's members with each other.
    """
    if len(group) < 2:
        return {}
    rows = reading_order(group)
    y_span = max(e.cy for e in group) - min(e.cy for e in group)
    x_span = max(e.cx for e in group) - min(e.cx for e in group)
    tolerance_y = 0.35 * min(e.height for e in group)
    tolerance_x = 0.35 * min(e.width for e in group)
    one_row = y_span <= tolerance_y
    one_column = x_span <= tolerance_x
    words: dict[str, str] = {}
    if one_row or one_column:
        ordered = sorted(group, key=lambda e: e.cx if one_row else e.cy)
        edges = ("leftmost", "rightmost") if one_row else ("top", "bottom")
        origin = "left" if one_row else "top"
        for rank, entry in enumerate(ordered):
            if rank == 0:
                words[entry.id] = edges[0]
            elif rank == len(ordered) - 1:
                words[entry.id] = edges[1]
            else:
                words[entry.id] = f"number {rank + 1} from the {origin}"
        return words
    mean_x = sum(e.cx for e in group) / len(group)
    mean_y = sum(e.cy for e in group) / len(group)
    for entry in rows:
        vertical = (
            "top"
            if entry.cy < mean_y - tolerance_y / 2
            else "bottom"
            if entry.cy > mean_y + tolerance_y / 2
            else ""
        )
        horizontal = (
            "left"
            if entry.cx < mean_x - tolerance_x / 2
            else "right"
            if entry.cx > mean_x + tolerance_x / 2
            else ""
        )
        words[entry.id] = " ".join(w for w in (vertical, horizontal) if w) or "middle"
    return words


def candidate_label(entry: VisualEntry, context: EditContext, where: str | None = None) -> str:
    """A visual named for a choice: "'Total MTD Sales' column chart, top left", plus its page name
    on a multi-page report."""
    page = context.page(entry.page_id)
    place = where or describe_position(entry, page)
    text = f"{entry.label} {type_name(entry.visual_type)}" if entry.caption else entry.label
    text = f"{text}, {place}"
    if len(context.pages) > 1:
        text += f", page '{entry.page_name}'"
    return text


def candidate_labels(candidates: list[VisualEntry], context: EditContext) -> list[str]:
    """Labels for a group of candidates that tell them apart by where they sit."""
    same_page = len({c.page_id for c in candidates}) == 1
    relative = relative_positions(candidates) if same_page else {}
    return [candidate_label(c, context, relative.get(c.id)) for c in candidates]


def ambiguity_question(
    candidates: list[VisualEntry], context: EditContext, noun: str = "visual"
) -> str:
    listed = candidate_labels(candidates[:5], context)
    tail = f" (or {len(candidates) - 5} more)" if len(candidates) > 5 else ""
    if len(listed) == 1:
        return f"Which {noun}: {listed[0]}{tail}?"
    return f"Which {noun}: " + "; ".join(listed[:-1]) + " or " + listed[-1] + tail + "?"


def _visible_pool(
    context: EditContext, include_hidden: bool, only_hidden: bool
) -> list[VisualEntry]:
    pool = [v for v in context.visuals if not v.is_group]
    if only_hidden:
        return [v for v in pool if v.hidden]
    return pool if include_hidden else [v for v in pool if not v.hidden]


def _compatible(entry: VisualEntry, cues: list[TypeCue]) -> bool:
    """Whether the entry is one of the types the request names (any of them)."""
    relevant = [c for c in cues if c.groups is not _ANY and c.groups]
    return not relevant or any(entry.visual_type in c.groups for c in relevant)


_MOVE_VERB = re.compile(
    r"\b(?:put|move|place|position|shift|send|bring|drag|slide|push|lift|lower|raise)\b", re.I
)
_EVERYTHING = re.compile(
    r"\b(?:everything|all\s+(?:of\s+)?(?:the\s+)?(?:visuals?|objects?|elements?|things?))\b", re.I
)
_DESTINATION = re.compile(
    r"\b(?:to|onto|into|after|before)\s+(?:the\s+)?(?:[\w'-]+\s+){0,2}$", re.I
)


_NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5}

# --- a name that matches nothing -----------------------------------------------------------------
#
# "delete the Zebra chart" names a chart that does not exist. Falling back to the canvas
# selection would delete the selected visual instead, so a name the report does not know is
# a question, not a "this".


def _wordset(text: str) -> frozenset[str]:
    return frozenset(text.split())


_DETERMINERS = _wordset("the this that these those my our a an all both every each")
_BOUNDARY = _wordset(
    "to in on of for by with and or then from into at as onto than so but also please it its "
    "them their"
)
# parts and properties a request may name instead of a visual ("hide the x axis")
_PART_WORDS = _wordset(
    "title heading caption subtitle legend label labels axis axes gridline gridlines border "
    "outline shadow background corner corners header headers value values number numbers "
    "font size color colour fill format width height position layout style x y"
)
_KNOWN_WORDS = (
    # position and count
    _wordset(
        "top bottom upper lower left right middle center centre central first second third "
        "fourth fifth sixth last leftmost rightmost topmost bottommost 1st 2nd 3rd 4th 5th next "
        "previous one two three four five six"
    )
    # describing a group or a type
    | _wordset(
        "far farthest furthest very wide narrow tall short long old existing original biggest "
        "largest smallest widest tallest tiniest nice simple basic pretty cool good better fancy "
        "quick extra brand additional selected current same whole entire other another new main "
        "big small large little visible hidden single only following stacked clustered "
        "horizontal vertical column bar line area pie donut combo kpi card table matrix pivot "
        "funnel gauge map filled data text page report dashboard canvas visual chart graph "
        "slicer image filter filters"
    )
    | _PART_WORDS
    # colors
    | _wordset(
        "red orange yellow green teal blue purple pink brown gray grey black white navy lime "
        "turquoise cyan aqua violet magenta maroon crimson gold silver"
    )
)
_MODEL_STOP = _wordset("date name id key value type count total sum")


def _words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9%#'&/-]+", text)


def _known_words(context: EditContext, entries: Iterable[VisualEntry]) -> set[str]:
    """Words of the captions and fields on the page, and of the page names."""
    words: set[str] = set()
    for entry in entries:
        for name in (entry.caption, *entry.fields):
            words.update(tokens(name))
    for page in context.pages:
        words.update(tokens(page.name))
    return words


def _visual_words(entry: VisualEntry) -> set[str]:
    words: set[str] = set()
    for name in (entry.caption, *entry.fields):
        words.update(tokens(name))
    return words


def _modifiers_before(text: str, cue: TypeCue) -> list[str]:
    """Words right before a type word ("the Zebra chart", "the discount chart") that no hue,
    position or type word explains: they must name something. Reading stops at a determiner,
    a verb or a preposition."""
    from .edit_text import EDIT_VERBS

    words = _words(text[: cue.span[0]])
    found: list[str] = []
    for word in reversed(words[-4:]):
        low = word.casefold()
        if low in _DETERMINERS or low in _BOUNDARY or re.fullmatch(EDIT_VERBS, low):
            break
        found.append(word)
    return [
        w
        for w in reversed(found)
        if w.casefold() not in _KNOWN_WORDS
        and not LITERAL_MARK.fullmatch(w)
        and not w.isdigit()
        and not re.fullmatch(r"\d+(?:st|nd|rd|th)", w, re.I)
    ]


_DESTRUCTIVE = re.compile(
    r"\b(?:delete|remove|drop|erase|get\s+rid\s+of|take\s+out|trash|kill|ditch|scrap|zap|toss|"
    r"nuke|lose|hide|duplicate|clone|copy)\b",
    re.I,
)


def _model_name_in(text: str, context: EditContext) -> str | None:
    """A measure, table or column name the text mentions, as it is spelled in the model."""
    plain = " " + " ".join(tokens(text)) + " "
    names = [m.name for m in context.measures] + list(context.tables)
    names += [c for columns in context.tables.values() for c in columns]
    for name in sorted(dict.fromkeys(names), key=len, reverse=True):
        wanted = tokens(name)
        if not wanted or (len(wanted) == 1 and wanted[0] in _MODEL_STOP):
            continue
        if " " + " ".join(wanted) + " " in plain:
            return name
    return None


def unknown_object(text: str, context: EditContext, known: set[str]) -> str | None:
    """What a request with no visual noun and no "it" points at when nothing in the report
    answers to it: a field the visuals do not show ("hide Profit"), or after a delete or
    hide verb any word nothing explains ("delete Zebra"). None when it names nothing odd."""
    from .edit_text import EDIT_VERBS

    if any(w.casefold() in _PART_WORDS for w in _words(text)):
        return None  # a part of the selected visual, not another visual
    named = _model_name_in(text, context)
    if named is not None and not set(tokens(named)) <= known:
        return named
    verb = _DESTRUCTIVE.search(text)
    if verb is None:
        return None
    found: list[str] = []
    for word in _words(text[verb.end() :]):
        low = word.casefold()
        if low in _DETERMINERS or low in _BOUNDARY or re.fullmatch(EDIT_VERBS, low):
            continue
        if (
            low in _KNOWN_WORDS
            or set(tokens(word)) <= known
            or word.isdigit()
            or LITERAL_MARK.fullmatch(word)
        ):
            continue
        found.append(word)
    return " ".join(found) if found else None


def _quantity(text: str, primary: TypeCue) -> int:
    """The number a request puts before its type word ("the two top charts"), or 0."""
    match = re.search(
        rf"\b(two|three|four|five)\s+(?:[\w-]+\s+){{0,3}}?{re.escape(primary.word)}", text, re.I
    )
    return _NUMBER_WORDS[match.group(1).casefold()] if match else 0


def _quantity_before_plural(text: str, types: list[TypeCue]) -> int:
    """The number before a plural type word that follows another ("the four KPI cards")."""
    for cue in types:
        if cue.plural and cue.strong:
            found = _quantity(text, cue)
            if found:
                return found
    return 0


def _rows(entries: list[VisualEntry]) -> list[list[VisualEntry]]:
    """The entries in rows, top row first, each row left to right."""
    left = list(entries)
    rows: list[list[VisualEntry]] = []
    while left:
        row = _band(left, "y", True)
        rows.append(sorted(row, key=lambda e: e.x))
        left = [e for e in left if e not in row]
    return rows


def _noun(primary: TypeCue | None) -> str:
    """The word to use when asking "Which chart?"."""
    if primary is None or primary.groups is _ANY:
        return "visual"
    if not primary.strong:
        return "chart"
    word = primary.word
    if word.endswith("ces"):
        return word[:-3] + "x"
    if word.endswith("xes"):
        return word[:-2]
    return word[:-1] if word.endswith("s") else word


def resolve_visuals(
    text: str,
    context: EditContext,
    *,
    carry: list[VisualEntry] | None = None,
    exclude: list[Span] | None = None,
    forced: list[str] | None = None,
    include_hidden: bool = False,
    only_hidden: bool = False,
    fits: Callable[[VisualEntry], bool] | None = None,
) -> Resolution:
    """The visual or visuals a request means.

    ``carry`` is the target of the clause before this one in the same message (for "it"
    and for a clause with no target of its own). ``exclude`` marks text that is a new value
    ("rename it to 'Sales'"), which must not be read as a name. ``fits`` narrows the list
    offered when the request names nothing at all.
    """
    result = Resolution(masked=text)
    pool_all = _visible_pool(context, include_hidden, only_hidden)
    if forced:
        wanted = set(forced)
        result.entries = [v for v in context.visuals if v.id in wanted]
        result.how, result.explicit = "forced", True
        return _mask(text, result)
    page, page_spans = find_page(text, context)
    if page is not None and any(_DESTINATION.search(text[: s[0]]) for s in page_spans):
        page, page_spans = None, []  # "duplicate it onto page 2": where it goes, not where it is
    result.page = page
    result.spans.extend(page_spans)
    active = _active_page(context)
    scope = page or active
    pool = [v for v in pool_all if scope is None or v.page_id == scope.id]
    result.pool = pool

    types = find_types(text)
    strong = [c for c in types if c.strong and c.groups is not _ANY]
    weak = [c for c in types if not c.strong]
    generic = [c for c in types if c.groups is _ANY]
    primary = strong[0] if strong else (weak[0] if weak else (generic[0] if generic else None))
    compat_cues = strong or weak
    moving = bool(_MOVE_VERB.search(text))
    # "the KPI cards at the top": the position follows the last of two type words in a row
    anchor = primary
    if primary is not None:
        anchor = next(
            (
                c
                for c in types
                if c.span[0] > primary.span[1] and not text[primary.span[1] : c.span[0]].strip()
            ),
            primary,
        )
    position, position_span = find_position(text, anchor, after=not moving)
    hue, hue_span = find_hue(text, primary)
    quantified = bool(_QUANTIFIER.search(text))
    both = bool(re.search(r"\bboth\b", text, re.I))
    plural = bool(primary and primary.plural and primary.strong)
    if primary is not None and not plural:
        # "the KPI cards": two type words in a row, the plural on the second
        plural = any(
            c is not primary
            and c.plural
            and c.strong
            and c.span[0] > primary.span[1]
            and not text[primary.span[1] : c.span[0]].strip()
            for c in types
        )
    result.pronoun = bool(_PRONOUN.search(text))
    result.type_noun = _noun(primary)
    result.spans.extend(s for s in (position_span, hue_span) if s)
    selection = context.visual(context.selection) if context.selection else None
    if selection is not None and (
        selection not in pool_all or (only_hidden and not selection.hidden)
    ):
        selection = None if only_hidden else selection

    def finish(entries: list[VisualEntry], how: str, explicit: bool) -> Resolution:
        result.entries, result.how, result.explicit = entries, how, explicit
        return _mask(text, result)

    def ambiguous(candidates: list[VisualEntry]) -> Resolution:
        result.candidates = candidates
        result.explicit = True
        result.question = ambiguity_question(candidates, context, result.type_noun)
        return _mask(text, result)

    # 1. A caption or field the request quotes, in full or a run of at least two of its words.
    #    A tie between visuals falls back to the canvas selection, else to a question.
    found = match_names(text, pool_all, exclude)
    scoped = [m for m in found if m.entry in pool] or ([] if page else found)
    matches = [m for m in scoped if _compatible(m.entry, compat_cues)]
    if matches:
        best = matches[0].tier
        chosen = [m for m in matches if m.tier == best]
        if best >= 3:
            longest = max(m.run for m in chosen)
            chosen = [m for m in chosen if m.run == longest]
        named = [m.entry for m in chosen]
        if position and primary and len(named) > 1:
            named = apply_position(named, position) or named
        result.spans.extend(m.span for m in chosen)
        how = "caption" if best in (1, 3) else "field"
        if len(named) == 1:
            return finish(named, how, True)
        if selection in named:
            return finish([selection], "selection", True)
        if plural:
            return finish(named, "all", True)
        return ambiguous(named)
    result.spans = [s for s in result.spans if s in (position_span, hue_span) or s in page_spans]

    # 2. Type, hue and position words describe a group; a plural or quantifier takes all of it.
    if primary is not None:
        group = [v for v in pool if _compatible(v, [primary])]
        # ("the Others bars" names a category in the data, so only a type word counts)
        named_by = _modifiers_before(text, primary) if primary.strong else []
        if named_by:
            # "the discount chart" names a word in a caption; "the Zebra chart" names nothing, and
            # a name nothing answers to is not "the selected one"
            narrowed = [
                v for v in group if all(set(tokens(w)) <= _visual_words(v) for w in named_by)
            ]
            page_words = _known_words(context, pool_all)
            if narrowed:
                group = narrowed
            elif all(set(tokens(w)) <= page_words for w in named_by):
                pass  # a word of another caption ("the agent table" for the EmployeeKey table)
            else:
                what = " ".join(named_by)
                result.explicit = True
                result.question = f"I could not find a {result.type_noun} called '{what}'." + (
                    " " + ambiguity_question(group, context, result.type_noun) if group else ""
                )
                result.candidates = group[:6] if len(group) > 1 else []
                return _mask(text, result)
        if not group and page is None:
            group = [v for v in pool_all if _compatible(v, [primary])]
        if hue:
            group = [v for v in group if hue in v.hues]
        if position:
            regional: list[VisualEntry] = []
            if plural and not position.ordinal and group:
                page_box = context.page(group[0].page_id)
                if page_box is not None:
                    regional = apply_region(group, position, page_box.width, page_box.height)
            banded = apply_position(group, position)
            # A half of the page that holds the whole group says nothing ("the left charts" when
            # every chart is left of center): then the row or column at that edge is meant.
            whole = len(regional) == len(group)
            one_word = not (position.vertical and position.horizontal)
            group = banded if not regional or (whole and one_word and len(banded) > 1) else regional
        # Fields the request also names narrow a group ("the chart with Total Sales").
        field_hits = [m.entry for m in match_names(text, group, exclude) if m.tier == 2]
        if field_hits and len(group) > 1:
            group = [v for v in group if v in field_hits]
        if not group:
            what = f"{hue} " if hue else ""
            result.question = f"I could not find a {what}{result.type_noun} that fits that."
            result.explicit = True
            return _mask(text, result)
        how = "position" if position else "hue" if hue else "type"
        if len(group) == 1:
            return finish(group, how, True)
        how_many = _quantity(text, primary) or _quantity_before_plural(text, types)
        if how_many and len(group) > how_many:
            # "the four cards" on a page with five: the row of exactly four, top row first
            rows = _rows(group)
            fitting = [row for row in rows if len(row) == how_many]
            if fitting:
                return finish(fitting[0], "all", True)
        if (both and len(group) != 2) or (how_many and len(group) != how_many):
            return ambiguous(group)  # "both slicers" on a page with five: which two?
        if selection in group and not (quantified or plural):
            return finish([selection], "selection", True)
        if quantified or plural:
            return finish(group, "all", True)
        return ambiguous(group)

    # 3. Only a pronoun, or nothing: the canvas selection, else the clause before.
    if quantified and _EVERYTHING.search(text):
        # "delete everything" is every visual on the page, never just the selected one
        return finish([v for v in pool if fits is None or fits(v)], "all", True)
    if selection is not None and not result.pronoun and not carry:
        odd = unknown_object(text, context, _known_words(context, pool_all))
        if odd:
            result.explicit = True
            result.question = (
                f"I could not find a visual called '{odd}' on this page. Select the visual on "
                "the canvas, or name it by its title."
            )
            return _mask(text, result)
    if selection is not None and (result.pronoun or not carry):
        return finish([selection], "selection", False)
    if carry:
        return finish(list(carry), "carry", False)
    if quantified and (_EVERYTHING.search(text) or not result.pronoun):
        # "remove all the borders" is every visual; "make them all blue" says nothing of which
        return finish([v for v in pool if fits is None or fits(v)], "all", True)
    offered = [v for v in pool if fits is None or fits(v)]
    result.explicit = False
    result.question = "Which visual? Select it on the canvas or name it" + (
        f", for example {_names(offered[:3])}." if offered else "."
    )
    result.candidates = offered[:6] if len(offered) > 1 else []
    return _mask(text, result)


def _mask(text: str, out: Resolution) -> Resolution:
    """Blank the words that named the target so property words are read from the rest."""
    chars = list(text)
    for start, end in out.spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    out.masked = "".join(chars)
    return out
