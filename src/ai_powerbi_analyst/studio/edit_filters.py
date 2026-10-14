"""Filters, read from chat: "only show 2019", "filter this page to Store and Online", "filter the
chart to the top 3 months by total sales", "limit this to the last 6 months", "remove the filter".

Each becomes one of ``set_visual_filter``, ``set_page_filter``, ``set_report_filter`` and
``clear_filter``. What the parser has to decide is which field, which values and which scope:

- the field is one the request names, or one its values point at (a year is the Year column, a
  month name the Month column); when nothing settles it, the question lists the candidate fields
  and every answer already carries its operation;
- the scope is the whole report when the request says so, a visual when it names one ("on this
  chart"), the page otherwise; a top or bottom N is per visual, so it asks for one;
- a filter is never guessed: without a value, a field or a number, it asks.

The operations check the field, the value types and the model themselves; this module only reads
words. It knows the columns of the model but not their values, so "Store and Online" alone does
not say which column holds them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .edit_context import EditContext, PageEntry, VisualEntry
from .edit_frame import Frame, names_phrase, need_entries
from .edit_ops import FieldHit, match_fields
from .edit_targets import find_types, resolve_visuals
from .edit_text import (
    PROPERTY_NOUNS,
    find_amounts,
    plain_number,
    quoted,
    scaled,
    short_number,
    tokens,
)
from .edit_types import Choice, Clause, ParseKind, Target

Op = dict[str, Any]

_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "fifteen": 15,
    "twenty": 20,
}
_COUNT = rf"(?:\d{{1,4}}|{'|'.join(_NUMBER_WORDS)})"
_UNIT = r"(?:day|week|month|quarter|year)s?"
_AMOUNT = r"-?\d[\d,]*(?:\.\d+)?\s*(?:k|m|b)?"

# Words that say a request is about a filter (to tell an edit from chat, and to read it).
FILTER_SIGNAL = re.compile(
    rf"\bfilter(?:s|ed|ing)?\b|\bonly\b|\bjust\s+(?:the\s+)?(?:\d|{'|'.join(_NUMBER_WORDS)})|"
    rf"\b(?:top|bottom)\s+{_COUNT}\b|\b(?:last|past|previous|next|coming)\s+(?:{_COUNT}\s+)?"
    rf"{_UNIT}\b|\bthis\s+(?:calendar\s+)?{_UNIT}\b|\bexclud\w+|\bexcept\b|"
    r"\b(?:limit|restrict)\w*\s+(?:it\s+|this\s+|the\s+\w+\s+)?to\b|\bbetween\b.+\band\b|\bunfilter",
    re.I,
)
# What turns a mention of a period or a number into an order to filter: a verb that asks for it.
_ORDER = re.compile(
    r"\b(?:only|just|filter|limit|restrict|keep|show|display|include|exclude|focus|narrow|"
    r"reduce|trim|drop|except)\b",
    re.I,
)


def looks_like_filter(text: str) -> bool:
    return FILTER_SIGNAL.search(text) is not None


# --- the model -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Column:
    table: str
    name: str
    kind: str  # "text" | "number" | "date"

    def ref(self) -> Op:
        return {"table": self.table, "name": self.name}

    @property
    def words(self) -> list[str]:
        return tokens(self.name)


def _kind(dtype: str) -> str:
    low = dtype.casefold()
    if "date" in low or "time" in low:
        return "date"
    if any(t in low for t in ("int", "float", "decimal", "number", "double")):
        return "number"
    return "text"


def _internal(name: str) -> bool:
    return name.startswith("_") or name.casefold().startswith(
        ("localdatetable", "dateautotemplate")
    )


def _columns(context: EditContext) -> list[Column]:
    return [
        Column(table, name, _kind(context.column_types.get(f"{table}.{name}", "")))
        for table, names in context.tables.items()
        if not _internal(table)
        for name in names
        if not _internal(name)
    ]


def _is_key(column: Column) -> bool:
    return column.name.casefold().endswith(("key", "id", "code"))


def _with_word(context: EditContext, word: str, kind: str | None = None) -> list[Column]:
    """The columns whose name has this word ("year" -> Date[Year]); one in a table named for
    dates comes before one in a fact table."""
    found = [c for c in _columns(context) if word in c.words and (kind is None or c.kind == kind)]
    return sorted(found, key=lambda c: 0 if re.search(r"date|calendar", c.table, re.I) else 1)


def _measures(context: EditContext) -> list[Column]:
    return [Column(m.table, m.name, "number") for m in context.measures if not _internal(m.name)]


_VISUAL_NOUN = re.compile(
    r"\s+(?:slicers?|cards?|charts?|graphs?|tables?|matri\w+|visuals?|kpis?)\b", re.I
)


def _hits(f: Frame) -> list[FieldHit]:
    """The fields the request names. "the ChannelName slicer" names a visual by its field, so
    a field word followed by a visual word is not a field."""
    return [
        h
        for h in match_fields(f.text, f.context, partial=True)
        if not _VISUAL_NOUN.match(f.text[h.end :])
    ]


def _column_of(f: Frame, hit: FieldHit) -> Column | None:
    return next(
        (c for c in _columns(f.context) if (c.table, c.name) == (hit.table, hit.name)), None
    )


def _active_page(context: EditContext) -> PageEntry | None:
    if context.active_page:
        return context.page(context.active_page)
    return context.pages[0] if len(context.pages) == 1 else None


# --- scope ---------------------------------------------------------------------------------------


@dataclass
class Scope:
    kind: str  # "report" | "page" | "visual"
    label: str
    page: PageEntry | None = None
    entries: list[VisualEntry] = field(default_factory=list)

    def target(self) -> Target:
        if self.kind == "visual":
            first = self.entries[0].page_id if self.entries else None
            return Target(tuple(e.id for e in self.entries), first, self.label, "selection")
        if self.kind == "page" and self.page is not None:
            return Target((), self.page.id, self.label, "page")
        return Target((), None, self.label, "all")


_REPORT = re.compile(
    r"\b(?:all\s+(?:the\s+)?pages|every\s+page|each\s+page|whole\s+(?:report|dashboard)|entire\s+"
    r"(?:report|dashboard)|report[- ](?:level|wide)|(?:across|throughout)\s+"
    r"(?:(?:the|this|that|my|our)\s+)?(?:report|dashboard|"
    r"pages)|everywhere|the\s+(?:report|dashboard))\b",
    re.I,
)
_THIS_PAGE = re.compile(r"\b(?:this|the|current|active)\s+page\b", re.I)


def _outside_quotes(text: str) -> str:
    """Keep offsets while hiding literal text from scope recognizers."""
    plain = list(text)
    for start, end, _inner in quoted(text):
        plain[start:end] = " " * (end - start)
    return "".join(plain)


def _ask_page(f: Frame) -> Clause:
    pages = f.context.pages[:8]
    choices = [Choice(label=p.name, fill={"append": f"on page {p.index + 1}"}) for p in pages]
    return f.clarify("Which page? " + ", ".join(f"'{p.name}'" for p in pages) + ".", choices)


def _scope(f: Frame, hits: list[FieldHit], prefer: str) -> Scope | Clause:
    """Where a filter goes: the report when the request says so, a visual it names or points at
    ("on this chart"), a page it names, else what the kind of request prefers: the page, or a
    visual (a top N, which ranks the values one visual shows)."""
    spans = [(h.start, h.end) for h in hits]
    plain = resolve_visuals(f.text, f.context, carry=f.carry, forced=f.forced.visuals)
    inside_a_field = all(any(a <= s and e <= b for a, b in spans) for s, e in plain.spans)
    if plain.how == "caption" and plain.ok and not (spans and inside_a_field):
        # a caption the request quotes ("the Total MTD Sales chart") names its visual
        f.res = plain
    else:
        # a field the request names ("by total sales") is not the name of a visual that shows it
        f.res = resolve_visuals(
            f.text, f.context, carry=f.carry, exclude=spans, forced=f.forced.visuals
        )
    res, low = f.res, _outside_quotes(f.low)
    if _REPORT.search(low) and res.page is None:
        return Scope("report", "the whole report")
    if res.page is not None or _THIS_PAGE.search(low):
        page = res.page or _active_page(f.context)
        return Scope("page", f"page '{page.name}'", page) if page else _ask_page(f)
    named = res.ok and res.explicit and res.how not in {"selection", "carry", "fit"}
    pointed = res.how == "selection" and res.ok and (res.pronoun or bool(find_types(f.text)))
    if named or pointed:
        return Scope("visual", names_phrase(res.entries), None, list(res.entries))
    if res.explicit and res.candidates and res.how not in {"selection", "carry"}:
        # "on the chart" with four charts on the page is a question, not the whole page
        return f.ask_target()
    if prefer == "visual" or (prefer == "clear" and res.how == "selection" and res.ok):
        entries = need_entries(f)
        if isinstance(entries, Clause):
            return entries
        return Scope("visual", names_phrase(entries), None, list(entries))
    page = _active_page(f.context)
    return Scope("page", f"page '{page.name}'", page) if page else _ask_page(f)


def _filter_ops(scope: Scope, spec: Op) -> list[Op]:
    if scope.kind == "report":
        return [{"op": "set_report_filter", "filter": spec}]
    if scope.kind == "page" and scope.page is not None:
        return [{"op": "set_page_filter", "page": scope.page.id, "filter": spec}]
    return [{"op": "set_visual_filter", "visual": e.id, "filter": spec} for e in scope.entries]


def _clause(f: Frame, scope: Scope, made: list[Op], summary: str) -> Clause:
    return Clause(
        f.text, ParseKind.APPLY, ops=tuple(made), summary=summary, targets=(scope.target(),)
    )


def _options(
    f: Frame,
    scope: Scope,
    columns: list[Column],
    build: Callable[[Column], Op],
    summary: Callable[[Column], str],
    question: str,
) -> Clause:
    """A question that lists the candidate fields; each answer is the finished operation."""
    if not columns:
        return f.not_available(
            "I could not find a field in the model that fits that. Name the field, for example "
            '"filter this page to 2019 on Year".'
        )
    names = [c.name for c in columns]
    choices = [
        Choice(
            label=f"{c.name} ({c.table})" if names.count(c.name) > 1 else c.name,
            ops=tuple(_filter_ops(scope, build(c))),
            summary=summary(c),
        )
        for c in columns[:6]
    ]
    return f.clarify(question, choices)


# --- clear ---------------------------------------------------------------------------------------

_CLEAR = re.compile(
    r"\b(?:remove|clear|delete|drop|reset|turn\s+off|switch\s+off|take\s+off|disable|undo|"
    r"get\s+rid\s+of|cancel|lift)\b.{0,40}?\bfilters?\b|\bunfilter\b|\bno\s+filters?\b|"
    r"\b(?:remove|clear|drop|reset)\s+(?:all\s+)?(?:the\s+)?filtering\b",
    re.I,
)


# "all" in "remove all filters" counts the filters, not the visuals: read as a visual reference it
# turned into one clear_filter per visual, which fails on the first visual without a filter.
_ALL_FILTERS = re.compile(
    r"\b(?:all|every|each|any)\s+(?:of\s+)?(?:the\s+|my\s+|these\s+|those\s+)?(?=filters?\b)", re.I
)


def _has_filters(entry: VisualEntry) -> bool:
    """Whether the visual holds a filter; True when the index does not carry its definition."""
    if entry.definition is None:
        return True
    config = entry.definition.get("filterConfig")
    return isinstance(config, dict) and bool(config.get("filters"))


def _ask_where_to_clear(f: Frame) -> Clause:
    """Ask which page, report or visual loses filters when no place is named."""
    page = _active_page(f.context)
    choices: list[Choice] = []
    if page is not None:
        choices.append(
            Choice(
                label=f"This page ('{page.name}')",
                ops=({"op": "clear_filter", "scope": "page", "target": page.id},),
                summary=f"Removed every filter from page '{page.name}'.",
            )
        )
    choices.append(
        Choice(
            label="The whole report",
            ops=({"op": "clear_filter", "scope": "report"},),
            summary="Removed every filter from the whole report.",
        )
    )
    holders = [
        e
        for e in f.context.visuals
        if e.definition is not None
        and _has_filters(e)
        and not e.is_group
        and (page is None or e.page_id == page.id)
    ]
    if holders:
        choices.append(
            Choice(
                label=f"The {len(holders)} visual{'s' if len(holders) != 1 else ''} with a filter",
                ops=tuple(
                    {"op": "clear_filter", "scope": "visual", "target": e.id} for e in holders
                ),
                summary=f"Removed every filter from {names_phrase(holders)}.",
            )
        )
    return f.clarify("Remove the filters from where?", choices)


def _clear(f: Frame) -> Clause | None:
    """ "remove the filter on Year", "clear all the filters on this page"."""
    if _CLEAR.search(_outside_quotes(f.low)) is None:
        return None
    if _ALL_FILTERS.search(f.low):
        text = _ALL_FILTERS.sub("", f.text)
        f = replace(f, text=text, low=text.casefold())
        place = resolve_visuals(text, f.context, carry=f.carry, forced=f.forced.visuals)
        said_where = (
            _REPORT.search(f.low) is not None
            or _THIS_PAGE.search(f.low) is not None
            or place.page is not None
        )
        if not (said_where or place.explicit or (place.how == "selection" and place.ok)):
            return _ask_where_to_clear(f)
    scope = _scope(f, [], "clear")
    if isinstance(scope, Clause):
        return scope
    # a field is named only by its whole name, and not by the words that name the visual
    spans = f.res.spans
    named_hits = [
        h for h in _hits(f) if h.tier == 1 and not any(s < h.end and h.start < e for s, e in spans)
    ]
    named = named_hits[-1] if named_hits else None
    if named is not None and named.kind == "measure":
        # a filter is removed by the column it filters; one on a measure goes with the others
        return f.not_available(
            f"I can remove a filter by the column it filters, not by a measure like "
            f"{named.name}. To take it off, clear every filter of {scope.label}, for example "
            '"clear all the filters on this page".'
        )
    made: list[Op] = []
    base: Op = {"op": "clear_filter", "scope": scope.kind}
    if named is not None:
        base["field"] = {"table": named.table, "name": named.name}
    if scope.kind == "report":
        made = [base]
    elif scope.kind == "page" and scope.page is not None:
        made = [{**base, "target": scope.page.id}]
    else:
        if named is None and len(scope.entries) > 1:
            # clearing a visual that holds no filter is an error that would stop the whole batch;
            # one named visual is left to the operation, which says there is nothing to remove
            holders = [e for e in scope.entries if _has_filters(e)]
            if not holders:
                return f.not_available(f"There are no filters on {scope.label} to remove.")
            scope.entries = holders
            scope.label = names_phrase(holders)
        made = [{**base, "target": e.id} for e in scope.entries]
    what = f"the filter on {named.name}" if named else "every filter"
    return _clause(f, scope, made, f"Removed {what} from {scope.label}.")


# --- top and bottom N ----------------------------------------------------------------------------

_TOP = re.compile(
    rf"\b(?P<dir>top|bottom|best|worst|highest|lowest)\s+(?P<n>{_COUNT})\b\s*(?P<rest>.*)$", re.I
)
_BY = re.compile(
    r"\b(?:by|ranked\s+by|based\s+on|according\s+to|in\s+terms\s+of|measured\s+by)\b", re.I
)


def _count(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBER_WORDS[word.casefold()]


def _top_spec(field_ref: Op, n: int, by: Op, direction: str) -> Op:
    return {"kind": "top_n", "field": field_ref, "n": n, "by": by, "direction": direction}


def _top_n(f: Frame) -> Clause | None:
    """ "filter the chart to the top 3 months by total sales"."""
    plain = _intent_plain(f)
    found = _TOP.search(plain)
    if found is None or not _ORDER.search(plain):
        return None  # "the top 3 charts" on its own is not a request for a filter
    if find_types(" ".join(found.group("rest").split()[:2])):
        return None  # "keep the top two charts" is about visuals, not about values
    hits = _hits(f)
    rest_at = found.start("rest")
    split = _BY.search(found.group("rest"))
    direction = "top" if found.group("dir").casefold() in {"top", "best", "highest"} else "bottom"
    n = _count(found.group("n"))
    if not 1 <= n <= 1000:
        return f.clarify("A top or bottom filter takes a number from 1 to 1000. How many?")
    by_at = rest_at + split.start() if split else None
    ranked_hits = [h for h in hits if by_at is not None and h.start >= by_at]
    grouped_hits = [h for h in hits if h.start >= rest_at and (by_at is None or h.end <= by_at)]
    measure = next((h for h in ranked_hits if h.kind == "measure"), None) or next(
        (h for h in hits if h.kind == "measure"), None
    )
    category = next((h for h in grouped_hits if h.kind == "column"), None) or next(
        (h for h in hits if h.kind == "column" and h.start < found.start()), None
    )
    if measure is None and category is None:
        return f.clarify(
            f'The {direction} {n} of what, ranked by which measure? For example "filter this '
            'chart to the top 3 months by Total Sales".'
        )
    scope = _scope(f, hits, "visual")
    if isinstance(scope, Clause):
        return scope

    def summary(category_name: str, measure_name: str) -> str:
        return f"Filtered {scope.label} to the {direction} {n} {category_name} by {measure_name}."

    if measure is not None and category is not None:
        spec = _top_spec(category.ref(), n, measure.ref(), direction)
        return _clause(f, scope, _filter_ops(scope, spec), summary(category.name, measure.name))
    if measure is None:
        assert category is not None
        return f.clarify(
            f"The {direction} {n} {category.name} by which measure?",
            [
                Choice(
                    label=m.name,
                    ops=tuple(_filter_ops(scope, _top_spec(category.ref(), n, m.ref(), direction))),
                    summary=summary(category.name, m.name),
                )
                for m in _measures(f.context)[:6]
            ],
        )
    ranked = measure
    return _options(
        f,
        scope,
        [c for c in _columns(f.context) if c.kind != "number" and not _is_key(c)],
        lambda c: _top_spec(c.ref(), n, ranked.ref(), direction),
        lambda c: summary(c.name, ranked.name),
        f"The {direction} {n} of which field, ranked by {ranked.name}?",
    )


# --- a period relative to today ------------------------------------------------------------------

_RELATIVE = re.compile(
    rf"\b(?P<dir>last|past|previous|next|coming|following|this|current)\s+"
    rf"(?:(?P<n>{_COUNT})\s+)?(?:(?P<cal>calendar|whole|full)\s+)?"
    r"(?P<unit>day|week|month|quarter|year)s?\b",
    re.I,
)


def _span_words(direction: str, unit: str, count: int, calendar: bool) -> str:
    if direction == "this":
        return f"this {unit}"
    if calendar and count == 1:
        return f"{direction} {unit}"
    return f"the {direction} {count} {unit}{'' if count == 1 else 's'}"


def _relative(f: Frame) -> Clause | None:
    """ "limit this to the last 6 months", "only show this year"."""
    plain = _intent_plain(f)
    found = _RELATIVE.search(plain)
    if found is None or not _ORDER.search(f"{plain[: found.start()]} {plain[found.end() :]}"):
        return None
    word = found.group("dir").casefold()
    direction = {"past": "last", "previous": "last", "coming": "next", "following": "next"}.get(
        word, word
    )
    direction = "this" if direction == "current" else direction
    unit_word = found.group("unit").casefold()
    if unit_word == "quarter":
        return f.clarify(
            "A relative date filter counts days, weeks, months or years. For a quarter, say the "
            'months: "the last 3 months".'
        )
    count = _count(found.group("n")) if found.group("n") else 1
    # "last month" and "this year" are calendar periods; "the last 6 months" is a rolling span
    calendar = unit_word != "day" and (
        found.group("cal") is not None
        or direction == "this"
        or (found.group("n") is None and word in {"last", "previous", "next"})
    )
    unit = f"calendar_{unit_word}s" if calendar else f"{unit_word}s"
    hits = _hits(f)
    dates = [c for c in _columns(f.context) if c.kind == "date"]
    named = [c for h in hits if (c := _column_of(f, h)) is not None and c.kind == "date"]
    if len(named) == 1:
        dates = named
    elif len(dates) > 1:
        # a calendar table's Date column beats another date column ("DateKey" of a fact table)
        home = [
            c
            for c in dates
            if re.search(r"date|calendar", c.table, re.I) and c.name.casefold() == "date"
        ]
        dates = home if len(home) == 1 else dates
    scope = _scope(f, hits, "page")
    if isinstance(scope, Clause):
        return scope
    span = _span_words(direction, unit_word, count, calendar)

    def build(column: Column) -> Op:
        return {
            "kind": "relative_date",
            "field": column.ref(),
            "unit": unit,
            "direction": direction,
            "count": count,
        }

    def summary(column: Column) -> str:
        return f"Filtered {scope.label} to {span} of {column.name}."

    if len(dates) != 1:
        return _options(f, scope, dates, build, summary, f"Which date field should {span} use?")
    return _clause(f, scope, _filter_ops(scope, build(dates[0])), summary(dates[0]))


# --- a number, a range --------------------------------------------------------------------------

_COMPARE = re.compile(
    r"(?P<op>>=|<=|>|<|\b(?:over|above|greater\s+than|more\s+than|higher\s+than|bigger\s+than|"
    r"larger\s+than|exceeding|at\s+least|no\s+less\s+than|minimum\s+of|under|below|less\s+than|"
    r"lower\s+than|fewer\s+than|smaller\s+than|at\s+most|no\s+more\s+than|maximum\s+of|"
    rf"equal\s+to|equals?|exactly)\b)\s*(?:of\s+)?(?P<amount>{_AMOUNT})\b",
    re.I,
)
_OPERATORS = (
    ("greater_or_equal", r">=|at\s+least|no\s+less\s+than|minimum\s+of"),
    ("less_or_equal", r"<=|at\s+most|no\s+more\s+than|maximum\s+of"),
    ("greater_than", r">|over|above|greater|more|higher|bigger|larger|exceeding"),
    ("less_than", r"<|under|below|less|lower|fewer|smaller"),
    ("is", r"equal|equals|exactly"),
)
_OPERATOR_WORDS = {
    "greater_than": "over",
    "greater_or_equal": "at least",
    "less_than": "under",
    "less_or_equal": "at most",
    "is": "equal to",
}
_BETWEEN = re.compile(rf"\bbetween\s+(?P<a>{_AMOUNT})\s+and\s+(?P<b>{_AMOUNT})\b", re.I)


def _intent_plain(f: Frame) -> str:
    """A named numeric field's words are not relative-date or ranking instructions.

    A bare period (even if a field is called ``Last Month``) remains ambiguous. A full
    name with words outside that cue, a field marker, or an adjacent comparison names
    the numeric operand. Partial date-word matches never hide an actual period.
    """
    plain = _outside_quotes(f.text)
    cues = [(m.start(), m.end()) for m in _RELATIVE.finditer(plain)]
    cues += [(m.start(), m.end("n")) for m in _TOP.finditer(plain)]
    masked = list(plain)
    for hit in match_fields(f.text, f.context):
        column = _column_of(f, hit)
        numeric = hit.kind == "measure" or (
            column is not None and column.kind == "number" and not _is_key(column)
        )
        if not numeric or _VISUAL_NOUN.match(f.text[hit.end :]):
            continue
        within = [(a, b) for a, b in cues if hit.start <= a and b <= hit.end]
        if not within:
            continue
        prefix = plain[: hit.start]
        marked = re.search(r"\b(?:field|measure|column)\s+(?:(?:named|called)\s+)?$", prefix, re.I)
        suffix = plain[hit.end :]
        bracketed = prefix.endswith("[") and suffix.startswith("]")
        gap = re.match(r"\s*(?:(?:values?|is|are|to|of)\s+)*", suffix, re.I)
        compared = gap is not None and (
            _COMPARE.match(suffix, gap.end()) or _BETWEEN.match(suffix, gap.end())
        )
        if marked or bracketed or compared or any(hit.start < a or b < hit.end for a, b in within):
            masked[hit.start : hit.end] = " " * (hit.end - hit.start)
    return "".join(masked)


def _operator(word: str) -> str:
    low = re.sub(r"\s+", " ", word.casefold())
    for name, pattern in _OPERATORS:
        if re.match(rf"(?:{pattern})", low):
            return name
    return "greater_than"


def _number(text: str) -> float | None:
    amounts = find_amounts(text)
    return scaled(amounts[0]) if amounts else None


def _plain(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _show(value: float) -> str:
    """A number as a person writes it: 100K, but a year stays 2019."""
    return plain_number(value) if 1900 <= value <= 2100 else short_number(value)


def _advanced(f: Frame) -> Clause | None:
    """A comparison or a range: "only show agents with total sales over 100K", "filter Year to
    between 2018 and 2020"."""
    plain = _outside_quotes(f.text)
    between = _BETWEEN.search(plain)
    compare = None if between else _COMPARE.search(plain)
    anchor = between or compare
    if anchor is None or not _ORDER.search(plain):
        return None
    conditions: list[Op]
    years = False  # a range of years names the Year column when the request names no field
    if between is not None:
        first, second = _number(between.group("a")), _number(between.group("b"))
        if first is None or second is None:
            return None
        low, high = min(first, second), max(first, second)
        years = low >= 1900 and high <= 2100
        conditions = [
            {"operator": "greater_or_equal", "value": _plain(low)},
            {"operator": "less_or_equal", "value": _plain(high)},
        ]
        words = f"between {_show(low)} and {_show(high)}"
    else:
        assert compare is not None
        value = _number(compare.group("amount"))
        if value is None:
            return None
        operator = _operator(compare.group("op"))
        conditions = [{"operator": operator, "value": _plain(value)}]
        words = f"{_OPERATOR_WORDS[operator]} {short_number(value)}"
    hits = _hits(f)
    usable = [
        h
        for h in hits
        if h.kind == "measure"
        or ((c := _column_of(f, h)) is not None and c.kind == "number" and not _is_key(c))
    ]
    before = [h for h in usable if h.end <= anchor.start()]
    pool = before or usable
    chosen = pool[-1] if pool else None
    field_ref: Op | None = {"table": chosen.table, "name": chosen.name} if chosen else None
    name = chosen.name if chosen else ""
    if chosen is None and years:
        year = next(iter(_with_word(f.context, "year", "number")), None)
        if year is not None:
            field_ref, name = year.ref(), year.name
    scope = _scope(f, hits, "page")
    if isinstance(scope, Clause):
        return scope

    def spec(ref: Op) -> Op:
        return {"kind": "advanced", "field": ref, "logic": "and", "conditions": conditions}

    if field_ref is not None:
        summary = f"Filtered {scope.label}: {name} {words}."
        return _clause(f, scope, _filter_ops(scope, spec(field_ref)), summary)
    return _options(
        f,
        scope,
        [
            *_measures(f.context),
            *(c for c in _columns(f.context) if c.kind == "number" and not _is_key(c)),
        ],
        lambda c: spec(c.ref()),
        lambda c: f"Filtered {scope.label}: {c.name} {words}.",
        "Which measure or number field is that about?",
    )


# --- values of a field ---------------------------------------------------------------------------

_INCLUDE = re.compile(
    r"\b(?:only\s+(?:show|display|include|keep|use|see)|show\s+only|display\s+only|keep\s+only|"
    r"include\s+only|just\s+(?:show|display|keep)|limit(?:ed)?\s+(?:it\s+|this\s+|the\s+\w+\s+)?to|"
    r"restrict(?:ed)?\s+(?:it\s+|this\s+|the\s+\w+\s+)?to|filter(?:ed)?\b.*?\b(?:to|for|on|by)|"
    r"filter|only|just)\b",
    re.I,
)
_EXCLUDE = re.compile(
    r"\b(?:exclud(?:e|ing)|except(?:\s+for)?|everything\s+but|all\s+but|leave\s+out|without|"
    r"not\s+including|other\s+than)\b",
    re.I,
)
_KINDS = (
    r"pages?|charts?|graphs?|visuals?|tables?|matri(?:x|ces)|slicers?|cards?|reports?|dashboards?"
)
_SCOPE_WORDS = re.compile(
    rf"\b(?:on|for|in|to|across|throughout)\s+(?:the\s+|this\s+|that\s+|my\s+|all\s+(?:the\s+)?|every\s+|"
    rf"each\s+)?(?:[\w'-]+\s+){{0,2}}?(?:{_KINDS})\b(?:\s+\d+)?|"
    rf"\b(?:this|the|current)\s+(?:{_KINDS})\b|\b(?:on|for|in)\s+it\b",
    re.I,
)
_FILLER = re.compile(
    r"^(?:the|a|an|only|just|data|values?|records?|rows?|results?|for|to|on|in|of|is|are|by|"
    r"from|out|where|with|equals?|=|:)$",
    re.I,
)
_JOINER = re.compile(r"^\s*(?:,|;|&|and\b|or\b)", re.I)
_TRAILING_JOINER = re.compile(r"(?:,|;|&|\band|\bor)\s*$", re.I)
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_MONTH_SHORT = {m[:3] for m in _MONTHS}


def _scope_word_spans(text: str, after: int = 0) -> list[tuple[int, int]]:
    """Scope phrases after the value marker, including quoted target names.

    Start after the marker so its ``to`` cannot bridge a masked value to ``on this page``.
    A quote follows a scope noun (``on page "Forecast"``), or a scope preposition
    (``on "Revenue"``), only when those words actually precede it.
    """
    plain = _outside_quotes(text)
    quotes = quoted(text)
    spans = []
    for match in _SCOPE_WORDS.finditer(plain, after):
        end = match.end()
        following = next((q for q in quotes if q[0] >= end), None)
        if following is not None and not text[end : following[0]].strip():
            end = following[1]
        spans.append((match.start(), end))
    for start, end, _ in quotes:
        if start < after:
            continue
        prefix = plain[after:start]
        before = re.search(r"\b(?:on|for|in|from)\s+(?:(?:the|this|that|my)\s+)?$", prefix, re.I)
        if before is not None:
            spans.append((after + before.start(), end))
    return spans


def _without_scope_words(text: str) -> str:
    """Remove complete scope phrases only outside quoted categorical literals."""
    plain = list(text)
    for start, end in _scope_word_spans(text):
        plain[start:end] = " " * (end - start)
    return "".join(plain)


def _split_values(text: str) -> list[str]:
    guarded = re.sub(r"\s+", " ", text).strip().strip(".!?,;: ")
    found: list[str] = []
    for start, end, inner in quoted(guarded):
        found.append(inner)
        guarded = guarded[:start] + " " * (end - start) + guarded[end:]
    for piece in re.split(r"\s*(?:,|;|&|\band\b|\bor\b)\s*", guarded):
        words = [w for w in piece.split() if not _FILLER.match(w)]
        if words:
            found.append(" ".join(words).strip("\"' "))
    return [v for v in dict.fromkeys(found) if v]


def _typed(value: str, kind: str) -> str | int | float | None:
    if kind == "number":
        amounts = find_amounts(value)
        if len(amounts) != 1 or amounts[0].text.strip() != value.strip():
            return None
        return _plain(scaled(amounts[0]))
    return value


def _infer_column(f: Frame, values: list[str]) -> Column | None:
    """The column a lone year, month or quarter points at, when the request names none."""
    lowered = [v.casefold() for v in values]
    if all(re.fullmatch(r"(?:19|20)\d\d", v) for v in values):
        return next(iter(_with_word(f.context, "year", "number")), None)
    if all(v in _MONTHS or v in _MONTH_SHORT for v in lowered):
        return next(iter(_with_word(f.context, "month", "text")), None)
    if all(re.fullmatch(r"q[1-4]", v) for v in lowered):
        return next(iter(_with_word(f.context, "quarter")), None)
    return None


def _field_hits(f: Frame, hits: list[FieldHit], after: int) -> list[FieldHit]:
    """The hits after ``after`` that name the field. A word of a field name that sits inside a
    list of values ("Store and Online") is a value, not the field: it touches a joiner."""
    text = f.text
    scope_spans = _scope_word_spans(text, after)
    literal_spans = [
        (start, end)
        for start, end, _ in quoted(text)
        if start >= after and not any(a <= start and end <= b for a, b in scope_spans)
    ]
    named: list[FieldHit] = []
    for hit in hits:
        if any(start <= hit.start and hit.end <= end for start, end in scope_spans):
            continue
        if any(start <= hit.start and hit.end <= end for start, end in literal_spans):
            continue
        if hit.start < after:
            # "filter ChannelName to Online": a field before the value marker is explicit,
            # including a quoted field name. Quoted values occur after that marker.
            named.append(hit)
            continue
        joined = bool(_JOINER.match(text[hit.end :])) or bool(
            _TRAILING_JOINER.search(text[after : hit.start])
        )
        if not joined:
            named.append(hit)
    return named


def _categorical(f: Frame) -> Clause | None:
    """ "only show 2019", "filter this page to ChannelName Store and Online", "exclude Online"."""
    include = None
    plain = _outside_quotes(f.text)
    for match in _INCLUDE.finditer(plain):
        include = match
    exclude = _EXCLUDE.search(plain)
    marker = exclude or include
    if marker is None:
        return None
    hits = _hits(f)
    named = _field_hits(f, hits, marker.end())
    blank = list(f.text[marker.end() :])
    for hit in named:
        for i in range(max(hit.start - marker.end(), 0), min(hit.end - marker.end(), len(blank))):
            blank[i] = " "
    value_text = _without_scope_words("".join(blank))
    values = _split_values(value_text)
    literal_free_values = _outside_quotes(value_text)
    if PROPERTY_NOUNS.search(literal_free_values) or find_types(literal_free_values):
        return None  # "only show the legend" is not a filter
    if not values:
        return f.clarify(
            'Filter to which values? For example "only show 2019" or "filter this page to '
            'ChannelName Store and Online".'
        )
    columns = [c for h in named if h.kind == "column" and (c := _column_of(f, h)) is not None]
    numeric_values = all(_typed(v, "number") is not None for v in values)
    if len(columns) > 1:
        fitting = [c for c in columns if (c.kind == "number") == numeric_values]
        columns = fitting or columns
    column = columns[-1] if columns else _infer_column(f, values)
    scope_text = list(f.text)
    scope_spans = _scope_word_spans(f.text, marker.end())
    for start, end, _ in quoted(f.text):
        in_scope = any(a <= start and end <= b for a, b in scope_spans)
        if start >= marker.end() and not in_scope:
            scope_text[start:end] = " " * (end - start)
    scope_frame = replace(f, text="".join(scope_text), low="".join(scope_text).casefold())
    scope_hits = [h for h in hits if not any(a <= h.start and h.end <= b for a, b in scope_spans)]
    scope = _scope(scope_frame, scope_hits, "page")
    if isinstance(scope, Clause):
        return scope
    if column is None and scope.kind == "visual" and len(scope.entries) == 1:
        # "only show Online in the ChannelName slicer": the slicer's one field is the field
        shown = [b for b in scope.entries[0].bindings if b[0] == "column"]
        column = next(
            (
                c
                for c in _columns(f.context)
                if len(shown) == 1 and (c.table, c.name) == shown[0][1:]
            ),
            None,
        )
    leave_out = exclude is not None

    def spec_for(c: Column) -> Op | None:
        typed = [_typed(v, c.kind) for v in values]
        if any(t is None for t in typed):
            return None
        return {"kind": "categorical", "field": c.ref(), "values": typed, "exclude": leave_out}

    def summary_for(c: Column) -> str:
        listed = ", ".join(values)
        if leave_out:
            return f"Filtered {scope.label} to leave out {listed} ({c.name})."
        return f"Filtered {scope.label} to {listed} ({c.name})."

    if column is not None:
        if column.kind == "date":
            return f.not_available(
                f"{column.name} is a date field: filter it by a period instead, for example "
                '"only show the last 6 months".'
            )
        made = spec_for(column)
        if made is None:
            what = "text" if column.kind == "text" else "numbers"
            return f.clarify(
                f"{column.name} holds {what}, and {', '.join(values)} does not fit it. Which "
                "field did you mean?"
            )
        return _clause(f, scope, _filter_ops(scope, made), summary_for(column))
    # no field named and none implied: the fields whose type fits the values are the options
    kind = "number" if numeric_values else "text"
    options = [
        c
        for c in _columns(f.context)
        if c.kind == kind and not _is_key(c) and spec_for(c) is not None
    ]
    return _options(
        f,
        scope,
        options,
        lambda c: spec_for(c) or {},
        summary_for,
        f"Which field holds {', '.join(values)}? I can filter on "
        + ", ".join(c.name for c in options[:6])
        + ".",
    )


# A request about something else that happens to use a filter word ("delete every page except
# Forecast") starts with its own verb and never says filter or only.
_OWN_VERB = re.compile(
    r"^\W*(?:please\s+)?(?:delete|remove|drop|hide|unhide|duplicate|copy|clone|rename|move|swap|"
    r"align|distribute|resize|bring|send|add|insert|create|color|colour|paint|change|set|turn)\b",
    re.I,
)
_FILTER_WORD = re.compile(r"\b(?:filter\w*|only|just|unfilter)\b", re.I)


def filter_clause(f: Frame) -> Clause | None:
    """A filter to set or clear: top and bottom N, a period, a range or comparison, values."""
    plain = _outside_quotes(f.text)
    if FILTER_SIGNAL.search(plain) is None:
        return None
    if _OWN_VERB.match(plain) and not _FILTER_WORD.search(plain):
        return None
    intent = _intent_plain(f)
    if _NOT_A_FILTER_NOW.search(intent):
        # "include last year in the comparison?" is about an answer; "filter by store later,
        # what stores are there" asks first and filters another time
        return None
    if _RELATIVE.search(intent) and (_COMPARE.search(intent) or _BETWEEN.search(intent)):
        return f.clarify(
            "That asks for both a relative date filter and a numeric condition. "
            "Please give each filter as a separate request so I can keep both conditions."
        )
    return _clear(f) or _top_n(f) or _relative(f) or _advanced(f) or _categorical(f)


_NOT_A_FILTER_NOW = re.compile(
    r"\b(?:in|to|from)\s+(?:the|your|this|that)\s+(?:comparison|answer|analysis|summary|reply|"
    r"response|explanation|numbers\s+you\s+gave)\b|"
    r"\b(?:later|tomorrow|next\s+(?:week|time)|some\s*day|eventually|at\s+some\s+point)\b|"
    r"\b(?:what|which|who|how\s+many)\b[^.!]*\b(?:are|is)\s+there\b",
    re.I,
)


__all__ = ["FILTER_SIGNAL", "filter_clause", "looks_like_filter"]
