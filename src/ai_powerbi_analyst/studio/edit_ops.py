"""Turn a parsed request into report_edits operations, one visual at a time.

The parser decides WHAT is asked (a move, a bigger visual, a field to add). These helpers
decide HOW for a particular visual: they resolve relative words ("bigger", "a bit wider") to
absolute numbers from the current file so a retry cannot apply them twice, and build the
operation dict exactly as report_edits validates it. A change a visual cannot take raises
``Unfit`` with a plain-English reason instead of producing an operation that would fail when
applied. Studio does not change styling, so nothing here builds a color, font, title, label,
legend, axis or box operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

from . import pbir_expr as expr
from .edit_context import EditContext, PageEntry, VisualEntry
from .edit_text import tokens
from .report_edits import REPORT_OPERATIONS

Op = dict[str, Any]


class Unfit(Exception):
    """The visual cannot take this change; the message says why in plain English."""


# --- answering a question the edit layer asked --------------------------------------------------

# The flag that answers each confirmation (``NeedsConfirmation.kind``): the edit layer stops
# before it loses something the person may want (a conditional format, the buttons and
# bookmarks that point at a page) and is told yes by a flag on the operation.
CONFIRMATION_FLAGS: dict[str, dict[str, bool]] = {
    "replace_conditional": {"replace_conditional": True},
    "remove_page_references": {"cleanup_references": True},
    # A bookmark that buttons or navigators still use, a drillthrough or tooltip page that
    # buttons or visuals still point at, a group a bookmark remembers: yes removes those links.
    "remove_bookmark_references": {"cleanup_references": True},
    "clear_page_links": {"cleanup_references": True},
    "cleanup_references": {"cleanup_references": True},
}


@cache
def _operation_fields() -> dict[str, frozenset[str]]:
    """Operation name -> the settings that operation takes."""
    found: dict[str, frozenset[str]] = {}
    for cls in REPORT_OPERATIONS:
        default = cls.model_fields["op"].default
        found[str(default)] = frozenset(cls.model_fields)
    return found


def ops_with_answer(
    made: Sequence[Mapping[str, Any]], answer: str | Mapping[str, Any], step: int | None = None
) -> list[Op]:
    """The operations with the answer to the edit layer's question filled in.

    ``answer`` is one of two things. The ``kind`` of a ``NeedsConfirmation`` says yes to it:
    ``"replace_conditional"``, or ``"remove_page_references"``, ``"remove_bookmark_references"``,
    ``"clear_page_links"`` and ``"cleanup_references"`` (which all set ``cleanup_references``).
    The ``fill`` of a ``Choice`` of a ``NeedsChoice`` is merged in: ``{"fields": [...]}`` for the
    aggregation of a number, a column or a table a family asks for. ``step`` is the 1-based step
    the question came from
    (``error.step``); the answer goes to that one. Without it, to the first operation that
    takes it (the batch stops at its first question, so that is the one that asked).

    Returns new dicts and never changes the ones given. Raises ValueError when no operation
    takes the answer, so a caller never re-sends a batch that would ask the same question."""
    fill: Mapping[str, Any]
    if isinstance(answer, str):
        if answer not in CONFIRMATION_FLAGS:
            raise ValueError(f"{answer!r} is not a question the edit layer asks")
        fill = CONFIRMATION_FLAGS[answer]
    else:
        fill = answer
    if not fill:
        raise ValueError("There is no answer to fill in")
    fields = _operation_fields()

    def takes(op: Mapping[str, Any]) -> bool:
        return all(key in fields.get(str(op.get("op")), frozenset()) for key in fill)

    if step is not None:
        if not 1 <= step <= len(made) or not takes(made[step - 1]):
            raise ValueError(f"Step {step} does not take that answer")
        wanted = {step - 1}
    else:
        # A batch stops at its first question, so the operation that asked is the first that
        # takes the answer; filling every one would answer questions nobody has asked yet (and
        # overwrite conditional formatting on visuals that were never reviewed).
        first = next((i for i, op in enumerate(made) if takes(op)), None)
        if first is None:
            raise ValueError("None of the operations takes that answer")
        wanted = {first}
    return [{**op, **fill} if i in wanted else dict(op) for i, op in enumerate(made)]


# --- geometry ------------------------------------------------------------------------------------


def _r(value: float) -> float:
    return round(value, 2)


def page_of(context: EditContext, entry: VisualEntry) -> PageEntry:
    page = context.page(entry.page_id)
    if page is None:
        raise Unfit("That visual's page is not in the report")
    return page


def rect_ops(
    entry: VisualEntry,
    page: PageEntry,
    *,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
) -> list[Op]:
    """One set_position with the new rectangle kept inside the page."""
    w = min(width if width is not None else entry.width, page.width)
    h = min(height if height is not None else entry.height, page.height)
    left = min(max(0.0, x if x is not None else entry.x), page.width - w)
    top = min(max(0.0, y if y is not None else entry.y), page.height - h)
    op: Op = {"op": "set_position", "visual": entry.id}
    if _r(left) != _r(entry.x):
        op["x"] = _r(left)
    if _r(top) != _r(entry.y):
        op["y"] = _r(top)
    if _r(w) != _r(entry.width):
        op["width"] = _r(w)
    if _r(h) != _r(entry.height):
        op["height"] = _r(h)
    if len(op) == 2:
        raise Unfit(f"{entry.label} is already there")
    return [op]


def scale_ops(entry: VisualEntry, page: PageEntry, fw: float, fh: float) -> list[Op]:
    """Grow or shrink from the top left corner; when the page edge stops the growth the
    visual slides back so that it still gets bigger."""
    width = min(entry.width * fw, page.width)
    height = min(entry.height * fh, page.height)
    return rect_ops(entry, page, width=max(width, 10.0), height=max(height, 10.0))


def nudge_step(page: PageEntry, horizontal: bool, scale: float = 1.0) -> float:
    return _r(max(10.0, (page.width if horizontal else page.height) * 0.03 * scale))


def gap(page: PageEntry) -> float:
    return _r(max(8.0, page.width * 0.006))


# --- adding visuals ------------------------------------------------------------------------------

ADD_TYPES: dict[str, str] = {
    "clustered column chart": "clusteredColumnChart",
    "column chart": "clusteredColumnChart",
    "stacked column chart": "columnChart",
    "100% stacked column chart": "hundredPercentStackedColumnChart",
    "clustered bar chart": "clusteredBarChart",
    "bar chart": "clusteredBarChart",
    "stacked bar chart": "barChart",
    "line chart": "lineChart",
    "area chart": "areaChart",
    "stacked area chart": "stackedAreaChart",
    "pie chart": "pieChart",
    "pie": "pieChart",
    "donut chart": "donutChart",
    "donut": "donutChart",
    "treemap": "treemap",
    "funnel": "funnel",
    "gauge": "gauge",
    "kpi": "kpi",
    "scatter chart": "scatterChart",
    "waterfall chart": "waterfallChart",
    "ribbon chart": "ribbonChart",
    "combo chart": "lineClusteredColumnComboChart",
    "card": "card",
    "multi-row card": "multiRowCard",
    "table": "tableEx",
    "matrix": "pivotTable",
    "slicer": "slicer",
    "chart": "clusteredColumnChart",
}


@dataclass(frozen=True)
class FieldHit:
    kind: str  # "measure" | "column"
    table: str
    name: str
    start: int
    end: int
    tier: int  # 1 full name, 2 one distinctive word of a longer name

    def ref(self) -> Op:
        return {"table": self.table, "name": self.name}


def _internal(name: str) -> bool:
    return name.startswith("_") or name.casefold().startswith(
        ("localdatetable", "dateautotemplate")
    )


_KEY_SUFFIX = ("key", "id", "code")


def match_fields(
    text: str,
    context: EditContext,
    exclude: Sequence[tuple[int, int]] = (),
    *,
    partial: bool = False,
) -> list[FieldHit]:
    """Measures and columns of the model that a request names, left to right.

    A whole name matches ("Total Sales"). With ``partial``, a single distinctive word of a
    longer column name matches too when no other column has it ("channel" -> ChannelName),
    never for key columns, which people do not put in charts by that word; that is only
    safe when the request is known to name fields ("a bar chart of Sales by channel").
    """
    import re

    words = [
        (w, m.start(), m.end())
        for m in re.finditer(r"[A-Za-z0-9]+", text)
        for w in tokens(m.group(0))
        if not any(s <= m.start() < e for s, e in exclude)
    ]
    plain = [w[0] for w in words]
    candidates: list[tuple[str, str, str]] = [
        ("measure", m.table, m.name) for m in context.measures if not _internal(m.name)
    ]
    candidates += [
        ("column", table, column)
        for table, columns in context.tables.items()
        if not _internal(table)
        for column in columns
        if not _internal(column)
    ]
    hits: list[FieldHit] = []
    single_words: dict[str, list[tuple[str, str, str, int, int]]] = {}
    for kind, table, name in candidates:
        wanted = tokens(name)
        if not wanted:
            continue
        # a run of the name's own words, in order
        for i in range(len(plain) - len(wanted) + 1):
            if plain[i : i + len(wanted)] == wanted:
                hits.append(
                    FieldHit(kind, table, name, words[i][1], words[i + len(wanted) - 1][2], 1)
                )
        if (
            partial
            and kind == "column"
            and len(wanted) > 1
            and not name.casefold().endswith(_KEY_SUFFIX)
        ):
            for i, word in enumerate(plain):
                if word in wanted and word not in {"name", "date", "type", "value", "number"}:
                    single_words.setdefault(word, []).append(
                        (kind, table, name, words[i][1], words[i][2])
                    )
    for entries in single_words.values():
        distinct = {(t, n) for _, t, n, _, _ in entries}
        if len(distinct) == 1:
            kind, table, name, start, end = entries[0]
            hits.append(FieldHit(kind, table, name, start, end, 2))
    # keep the longest non-overlapping hits, best tier first
    hits.sort(key=lambda h: (-(h.end - h.start), h.tier, h.start))
    chosen: list[FieldHit] = []
    for hit in hits:
        if any(hit.start < c.end and c.start < hit.end for c in chosen):
            continue
        # a measure and a column may share a name; the measure wins (Power BI does the same)
        chosen.append(hit)
    return sorted(chosen, key=lambda h: h.start)


def is_date_column(context: EditContext, table: str, column: str) -> bool:
    kind = context.column_types.get(f"{table}.{column}", "").casefold()
    return "date" in kind or "time" in kind or column.casefold() in {"date", "month", "year"}


def chart_title(hits: list[FieldHit], categories: list[FieldHit]) -> str:
    measures = ", ".join(h.name for h in hits)
    if categories:
        return f"{measures} by {', '.join(h.name for h in categories)}"
    return measures


# --- roles ---------------------------------------------------------------------------------------


def role_of_field(entry: VisualEntry, name: str) -> str | None:
    """The field well (role) a field sits in on this visual."""
    state = (entry.definition or {}).get("visual", {}).get("query", {}).get("queryState", {})
    wanted = name.casefold()
    for role, bucket in state.items():
        for projection in bucket.get("projections", []):
            node = projection.get("field", {})
            found = node.get("Measure") or node.get("Column") or {}
            if not found:
                found = node.get("Aggregation", {}).get("Expression", {}).get("Column", {})
            names = {
                str(found.get("Property", "")).casefold(),
                str(projection.get("nativeQueryRef", "")).casefold(),
            }
            if wanted in names:
                return str(role)
    return None


def role_display(visual_type: str, role: str) -> str:
    try:
        roles = expr.roles(visual_type)["roles"]
    except expr.ExpressionError:
        return role
    return str(roles.get(role, {}).get("displayName", role))


def default_role(entry: VisualEntry, kind: str) -> str:
    """Where a newly added field goes: a measure to the measure well, a column to the first
    grouping well that is still free (or the first one)."""
    try:
        roles = expr.roles(entry.visual_type)["roles"]
        order = expr.role_order(entry.visual_type)
    except expr.ExpressionError as error:
        raise Unfit(f"Studio cannot add fields to {entry.label}") from error
    used = {r for r, _, _ in _occupied(entry)}
    wanted = {
        "measure": ("Measure", "GroupingOrMeasure"),
        "column": ("Grouping", "GroupingOrMeasure"),
    }
    options = [
        r
        for r in order
        if roles[r].get("kind") in wanted[kind]
        and r != "Tooltips"
        and roles[r].get("displayName") != "Small multiples"
    ]
    if not options:
        raise Unfit(f"{entry.label} has no place for a {kind}")
    free = [r for r in options if r not in used]
    return (free or options)[0]


def _occupied(entry: VisualEntry) -> list[tuple[str, str, str]]:
    state = (entry.definition or {}).get("visual", {}).get("query", {}).get("queryState", {})
    return [
        (str(role), "", str(p.get("nativeQueryRef", "")))
        for role, bucket in state.items()
        for p in bucket.get("projections", [])
    ]


# --- number formats ------------------------------------------------------------------------------


def format_string(kind: str, decimals: int | None = None, currency: str = "$") -> str:
    """A Power BI format string for "percent", "currency", "thousands", "millions",
    "whole" or "number" with an optional decimal count."""
    places = "" if not decimals else "." + "0" * decimals
    if kind == "percent":
        return f"0{places or '.0'}%" if decimals is None else f"0{places}%"
    if kind == "currency":
        return f"{currency}#,0{places or '.00'}" if decimals is None else f"{currency}#,0{places}"
    if kind == "thousands":
        return f'#,0{places},"K"'
    if kind == "millions":
        return f'#,0{places},,"M"'
    if kind == "whole":
        return "0"
    return f"#,0{places}"
