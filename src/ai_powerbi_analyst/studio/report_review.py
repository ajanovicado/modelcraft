"""Look at a page the way a Power BI developer glances at the canvas after every change.

The local model cannot see the canvas, so Studio checks the page itself and says what a
developer would notice: visuals that cover each other or run off the page, edges that almost
line up, visuals too small to read or with no data, two slicers on one field, one measure shown
with different number formats. Everything here is deterministic: no model call makes or words a
finding. Styling (fonts, colours, contrast, titles, legends) is not reviewed: Studio does not
change it, so it offers no finding it could only answer with "do it in the Format pane".

- ``review_page(document, page_id, payload=None, focus=None)`` checks one page and returns a
  ``Review``. ``payload`` is the page's render payload from ``live_report`` (``render_page``
  or ``render_formatting_payload``): with it the review reads the renderer's resolved tokens,
  the rows (empty visuals, pie slices), the number formats and the errors.
  Without it the tokens are resolved from the document through the renderer's own token
  functions (``report_tokens``), and the rules that need rows are listed as skipped.
  ``focus`` is the visuals an edit touched: they are the ones a fix moves ("snap the edited
  visual to its neighbours"), and their findings come first.
- ``review_edit(before, after)`` keeps only what an edit introduced or made worse, so the
  receipt of an edit never nags about what the report already had; the old issues are what an
  explicit "review this page" lists.

A ``Finding`` names its visuals in words (never by id), says in one sentence what is wrong and,
when the fix is mechanical, carries it as operation dicts that parse with the edit families'
parsers (``set_position``, ``distribute_visuals``, ``set_number_format``). A fix is never
offered when the right one is a judgement call (which of two overlapping visuals to move, which
slicer is the wrong one); ``fix_summary`` then says what to look at.

Positions are page pixels (a group member's stored position is relative to its group; the
geometry helpers add the origins). Font sizes in the tokens are CSS px; messages use points, as
Power BI does (1 pt = 4/3 px).

Tuned on the real Agents Performance dashboard (Desktop-authored, 15 visuals): it gets a
handful of true findings (a chart 1.4 px off the grid, a slicer 1 px taller than its
neighbours, two slicers on the same field) and none from the deliberate composition around it
(a card used as the label of a slicer, charts sitting on a panel drawn in the page picture).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard

from . import report_edit_validation as validation
from . import report_tokens as tokens
from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_captions import DYNAMIC_TITLE, find_caption_overlays
from .report_edit_geometry import (
    TOLERANCE,
    ancestors,
    describe,
    is_group,
    join_names,
    page_box,
    sentence,
    show,
    type_word,
)
from .report_props import number as prop_number
from .report_roles import Box, default_size, overlap_area, projection_label
from .report_theme import DEFAULT_THEME, deep_merge, theme_palette

Json = dict[str, Any]
Severity = Literal["problem", "suggestion"]

# --- the rules --------------------------------------------------------------------------------

# Stable ids (the UI and review_edit key on them) with the plain name a list shows.
RULES: dict[str, str] = {
    "visual_error": "Visuals that show an error",
    "empty_visual": "Visuals that show no data",
    "off_canvas": "Visuals outside the page or cut off by its edge",
    "overlap": "Visuals that cover each other",
    "too_small": "Visuals too small to read",
    "same_field_slicers": "Slicers that filter the same field",
    "near_miss": "Edges that almost line up",
    "uneven_gaps": "Uneven spacing in a row or column",
    "mixed_number_formats": "One measure shown with different number formats",
    "too_many_visuals": "Too many visuals on one page",
    "pie_slices": "Pie or donut charts with too many slices",
    "needs_power_bi": "Visuals only Power BI can draw",
}
_ORDER = {rule: index for index, rule in enumerate(RULES)}
# The rules that read rows: without a payload that has them they cannot run.
_ROW_RULES = ("empty_visual", "pie_slices")
# The rules that read what only the renderer knows (errors, the model's number formats).
_PAYLOAD_RULES = ("visual_error", "needs_power_bi", "mixed_number_formats")

# --- thresholds (each with the reason it has this value) ------------------------------------

# Below a pixel is float noise, not a misplacement: Desktop writes positions with 14 to 17
# digits and a page scaled from another size leaves 0.3 to 0.6 px differences nobody placed
# (the Agents chart grid has two). Further apart than 6 px looks deliberate.
NEAR_MISS_MIN = 1.0
NEAR_MISS_MAX = 6.0
# Two visuals are in one row (or column) when they share this much of the smaller one's
# height (width); less and they are only diagonal neighbours.
NEIGHBOUR_SHARE = 0.25
# Gaps in a row that differ by more than this separate groups on purpose (two clusters of
# cards); up to it, they were meant to be equal.
UNEVEN_GAP_MAX = 24.0
# A data visual a reader can still read: the validation's advisory minimum (20 px a side) for
# any data visual, and for a chart room for its title, axis labels and some plot. The chart
# size is a design threshold, not a measurement.
CHART_MIN_WIDTH = 150.0
CHART_MIN_HEIGHT = 100.0
# Line height of text as a share of its font size (Segoe UI's ascent + descent is ~1.33 em;
# 1.2 keeps the estimate on the side of "it fits").
LINE_HEIGHT = 1.2
# Desktop's default inner padding of a visual, used when the visual sets none.
DEFAULT_PADDING = 5.0
# Every data visual sends at least one query when the page opens and after every slicer
# change; past about 20 the page is slow and hard to scan (the Agents page, a typical
# dashboard, has 15, all with data).
MAX_VISUALS = 20
# Past eight slices a reader can no longer match them to a legend.
MAX_SLICES = 8
# Past this many visuals the pairwise layout checks are skipped (the validation's own cap).
MAX_LAYOUT_VISUALS = 120
# review_edit reports a finding the report already had only when the edit made it worse by at
# least this much, in the unit of the rule's ``amount`` (px, a share, a contrast ratio, a count).
_WORSE = {
    "overlap": 0.05,
    "off_canvas": 1.0,
    "too_small": 1.0,
    "near_miss": 0.5,
    "uneven_gaps": 1.0,
    "too_many_visuals": 1.0,
    "pie_slices": 1.0,
}

_CHART_FAMILIES = frozenset({"chart"})
_PIES = frozenset({"pieChart", "donutChart"})
_CARD_FAMILIES = frozenset({"card", "cardVisual"})
_SLICER_FAMILIES = frozenset({"slicer", "advancedSlicer"})
# --- results ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """One thing a developer would notice on the page.

    ``fix`` is a list of operation dicts (parsed by the edit families' parsers before it is
    offered), or None when the right fix is a judgement call; ``fix_summary`` says what the fix
    does, or what to look at instead. ``key`` identifies the finding across edits (rule, the
    visuals it is about, what of them); ``amount`` is how bad it is in the rule's own unit, so
    ``review_edit`` can tell "made worse" from "unchanged"."""

    id: str
    rule: str
    severity: Severity
    visuals: list[str]
    page: str
    message: str
    fix: list[dict[str, Any]] | None
    fix_summary: str
    key: str = field(default="", repr=False)
    amount: float = field(default=1.0, repr=False)

    def to_dict(self) -> Json:
        return {
            "id": self.id,
            "rule": self.rule,
            "severity": self.severity,
            "visuals": list(self.visuals),
            "page": self.page,
            "message": self.message,
            "fix": [dict(op) for op in self.fix] if self.fix is not None else None,
            "fix_summary": self.fix_summary,
        }


@dataclass(frozen=True)
class Review:
    """What a look at one page found. ``checked`` lists the rules that ran; ``skipped`` the
    ones that could not (rule -> the plain reason); ``notes`` what a rule could not check for
    some visuals (text over a background picture)."""

    page: str
    page_name: str
    findings: list[Finding]
    checked: list[str]
    skipped: dict[str, str]
    notes: list[str]
    focus: list[str]

    @property
    def summary(self) -> Json:
        by_rule = Counter(f.rule for f in self.findings)
        return {
            "problems": sum(1 for f in self.findings if f.severity == "problem"),
            "suggestions": sum(1 for f in self.findings if f.severity == "suggestion"),
            "by_rule": {rule: by_rule[rule] for rule in RULES if by_rule[rule]},
        }

    def to_dict(self) -> Json:
        return {
            "page": self.page,
            "page_name": self.page_name,
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
            "checked": list(self.checked),
            "skipped": dict(self.skipped),
            "notes": list(self.notes),
            "focus": list(self.focus),
        }


# --- the view of one visual --------------------------------------------------------------------


@dataclass
class _View:
    id: str
    visual: Json
    visual_type: str
    family: str
    box: Box
    z: float
    fmt: Json
    row: Json | None
    projections: list[Json]
    words: str
    order: int = 0

    @property
    def is_data(self) -> bool:
        return bool(self.projections)


def _projection_fields(projections: list[tuple[str, Json]]) -> list[Json]:
    fields: list[Json] = []
    for role, projection in projections:
        spec = projection.get("field")
        spec = spec if isinstance(spec, dict) else {}
        kind = "measure" if ("Measure" in spec or "Aggregation" in spec) else "column"
        fields.append(
            {
                "label": projection_label(projection),
                "kind": kind,
                "role": role,
                "query_ref": projection.get("queryRef"),
                "hidden": projection.get("hidden") is True,
            }
        )
    return fields


def _roles(visual: Json) -> list[tuple[str, Json]]:
    body = visual.get("visual")
    query = body.get("query") if isinstance(body, dict) else None
    state = query.get("queryState") if isinstance(query, dict) else None
    found: list[tuple[str, Json]] = []
    for role, section in state.items() if isinstance(state, dict) else []:
        projections = section.get("projections") if isinstance(section, dict) else None
        for projection in projections if isinstance(projections, list) else []:
            if isinstance(projection, dict):
                found.append((str(role), projection))
    return found


def _document_format(
    visual_type: str, body: Json, context: tokens.FormatContext, fields: list[Json]
) -> Json:
    """The tokens ``live_report`` would send for this visual, from the renderer's own token
    functions (no archive, no query): what the review reads when it has no payload."""
    reader = tokens.Reader(visual_type, body, context)
    family = tokens.family_of(visual_type)
    title = tokens.title_token(reader, family, fields)
    fmt: Json = {"title": title, "container": tokens.container_token(reader)}
    first = fields[0].get("query_ref") if fields else None
    first_label = str(fields[0]["label"]) if fields else None
    if family == "chart":
        fmt.update(tokens.chart_tokens(reader, visual_type))
    elif family == "cardVisual":
        card = tokens.card_visual_tokens(reader, first, first_label)
        fmt.update({"cardValue": card["value"], "cardLabel": card["label"], "tile": card["tile"]})
    elif family == "card":
        card = tokens.legacy_card_tokens(reader)
        fmt.update({"cardValue": card["value"], "cardLabel": card["label"], "tile": None})
    elif family == "table":
        fmt.update(tokens.table_tokens(reader))
    elif family in _SLICER_FAMILIES:
        label = reader.text("header", "text", None) or title["text"] or first_label
        slicer = tokens.slicer_tokens(reader, visual_type, label)
        fmt["tile"] = slicer["slicer"].pop("tile", None)
        fmt.update(slicer)
    elif family == "textbox":
        fmt["content"] = tokens.textbox_content(reader)
    elif family == "shape":
        fmt["shape"] = tokens.shape_token(reader)
    return fmt


def _theme(document: PbirDocument) -> Json:
    """The effective theme as ``report_theme.merged_theme`` builds it from the archive: the
    base theme over Desktop's default, then the custom theme. The document is in memory (an
    edit not yet written), so it is merged from the document's own theme files."""
    base = document.base_theme()
    merged = deep_merge(DEFAULT_THEME, base) if base else deep_merge(DEFAULT_THEME, {})
    custom = document.custom_theme()
    return deep_merge(merged, custom[1]) if custom else merged


def _num(value: Any, default: float) -> float:
    found = prop_number(value)
    return default if found is None else found


def _hidden(page: PageDoc, name: str) -> bool:
    """Hidden itself or inside a hidden group (a group's flag reaches its members)."""
    return any(page.visuals[n].get("isHidden") is True for n in (name, *ancestors(page, name)))


def _payload_rows(payload: Json | None, page: PageDoc) -> tuple[dict[str, Json], bool]:
    """The payload's rows by visual id, and whether they carry data (a ``render_page``
    answer) rather than formatting only. A payload of another page is not used."""
    if not payload:
        return {}, False
    page_info = payload.get("page")
    if isinstance(page_info, dict) and page_info.get("id") not in (None, page.name):
        return {}, False
    visuals = payload.get("visuals")
    rows: dict[str, Json] = {}
    if isinstance(visuals, list):
        for item in visuals:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                rows[item["id"]] = item
    elif isinstance(visuals, dict):
        rows = {str(k): v for k, v in visuals.items() if isinstance(v, dict)}
    has_data = any("rows" in row for row in rows.values())
    return rows, has_data


# --- the context of one review ------------------------------------------------------------------


@dataclass
class _Context:
    document: PbirDocument
    page: PageDoc
    views: list[_View]  # visible, non-group visuals in reading order
    by_id: dict[str, _View]
    focus: frozenset[str]
    has_rows: bool
    has_payload: bool
    overlays: dict[str, list[str]]  # host id -> the text-only visuals laid over it
    overlay_ids: frozenset[str]
    notes: list[str] = field(default_factory=list)

    def title(self, view: _View) -> str:
        return sentence(view.words)


def _build_context(
    document: PbirDocument, page: PageDoc, payload: Json | None, focus: Iterable[str]
) -> _Context:
    theme = _theme(document)
    context = tokens.FormatContext(theme, theme_palette(theme.get("dataColors", [])))
    rows, has_rows = _payload_rows(payload, page)
    views: list[_View] = []
    for name, visual in page.visuals.items():
        if is_group(visual) or _hidden(page, name):
            continue
        body = visual.get("visual")
        body = body if isinstance(body, dict) else {}
        visual_type = str(body.get("visualType", ""))
        roles = _roles(visual)
        fields = _projection_fields(roles)
        row = rows.get(name)
        if row is not None and row.get("visual_type") not in (None, visual_type):
            row = None  # a payload drawn before the edit changed the visual's type
        fmt = row.get("formatting") if row is not None else None
        if not isinstance(fmt, dict) or "title" not in fmt:
            try:
                fmt = _document_format(visual_type, body, context, fields)
            except (KeyError, TypeError, ValueError, AttributeError):
                fmt = {}
        position = visual.get("position")
        z = prop_number(position.get("z"), 0.0) if isinstance(position, dict) else 0.0
        views.append(
            _View(
                id=name,
                visual=visual,
                visual_type=visual_type,
                family=tokens.family_of(visual_type),
                box=page_box(page, name),
                z=z or 0.0,
                fmt=fmt,
                row=row,
                projections=[p for _, p in roles],
                words=describe(visual, page),
            )
        )
    views.sort(key=lambda v: (round(v.box.y, 1), round(v.box.x, 1), v.id))
    for index, view in enumerate(views):
        view.order = index
    visible = {v.id for v in views}
    overlays: dict[str, list[str]] = {}
    for overlay, host in find_caption_overlays(page.visuals.values()):
        if overlay in visible and host in visible:
            overlays.setdefault(host, []).append(overlay)
    overlay_ids = frozenset(o for found in overlays.values() for o in found)
    return _Context(
        document=document,
        page=page,
        views=views,
        by_id={v.id: v for v in views},
        focus=frozenset(focus),
        has_rows=has_rows,
        has_payload=bool(rows),
        overlays=overlays,
        overlay_ids=overlay_ids,
    )


# --- building findings ---------------------------------------------------------------------------


def _checked(ops: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """``ops`` when every one parses with the edit families' parsers, else None: a fix Studio
    could not apply is never offered. The facade parses the registered families; a family the
    integration has not registered yet (``set_number_format``) parses with its own parser."""
    # Imported when a fix is built: the facade brings every family with it.
    from . import report_edits

    try:
        report_edits.parse_operations(ops)
        return ops
    except DocumentError:
        pass
    try:
        from .report_edit_conditional import parse_conditional

        parse_conditional(ops)
    except (DocumentError, ImportError):
        return None
    return ops


def _finding(
    ctx: _Context,
    rule: str,
    severity: Severity,
    visuals: Sequence[str],
    message: str,
    fix_summary: str,
    *,
    detail: str = "",
    fix: list[dict[str, Any]] | None = None,
    amount: float = 1.0,
) -> Finding:
    key = "|".join((rule, ",".join(sorted(visuals)), detail))
    digest = hashlib.sha256(f"{ctx.page.name}|{key}".encode()).hexdigest()[:10]
    return Finding(
        id=f"{rule}-{digest}",
        rule=rule,
        severity=severity,
        visuals=list(visuals),
        page=ctx.page.name,
        message=message,
        fix=_checked(fix) if fix else None,
        fix_summary=fix_summary,
        key=key,
        amount=amount,
    )


def _pt(px_value: float) -> float:
    """Power BI points for a CSS px size, to the half point the Format pane shows."""
    return round(px_value * 0.75 * 2) / 2


def _pt_text(px_value: float) -> str:
    return f"{_pt(px_value):g} pt"


# --- rules: what the renderer says ------------------------------------------------------------


def _rule_errors(ctx: _Context) -> list[Finding]:
    found: list[Finding] = []
    for view in ctx.views:
        row = view.row
        if row is None:
            continue
        error = row.get("error")
        reason = row.get("unsupported_reason")
        if isinstance(reason, str) and reason:
            found.append(
                _finding(
                    ctx,
                    "needs_power_bi",
                    "suggestion",
                    [view.id],
                    f"{ctx.title(view)} is only previewed in Power BI: {reason.rstrip('.')}.",
                    "Check it in Power BI Desktop.",
                    detail="unsupported",
                )
            )
        elif isinstance(error, str) and error and row.get("needs_desktop"):
            found.append(
                _finding(
                    ctx,
                    "needs_power_bi",
                    "suggestion",
                    [view.id],
                    f"Studio cannot draw {view.words} ({error.rstrip('.')}).",
                    "Preview the page in Power BI Desktop to check it.",
                    detail="needs_desktop",
                )
            )
        elif isinstance(error, str) and error:
            found.append(
                _finding(
                    ctx,
                    "visual_error",
                    "problem",
                    [view.id],
                    f"{ctx.title(view)} shows an error: {error.rstrip('.')}.",
                    "Check the fields and filters it uses.",
                )
            )
    return found


def _measure_indexes(row: Json) -> list[int]:
    columns = row.get("columns")
    return [
        i
        for i, c in enumerate(columns if isinstance(columns, list) else [])
        if isinstance(c, dict)
        and c.get("kind") == "measure"
        and not c.get("hidden")
        and c.get("role") not in ("Formatting", "Tooltips")
    ]


def _rule_empty(ctx: _Context) -> list[Finding]:
    found: list[Finding] = []
    for view in ctx.views:
        row = view.row
        if row is None or "rows" not in row or row.get("error") or not view.is_data:
            continue
        if view.family not in _CHART_FAMILIES | _CARD_FAMILIES | _SLICER_FAMILIES | {"table"}:
            continue
        if view.family in _CARD_FAMILIES and not _shown(view.fmt.get("cardValue")):
            continue  # a card used as a label: its number is switched off
        data = row.get("rows")
        data = data if isinstance(data, list) else []
        if not data:
            words = (
                "has nothing to choose from" if view.family in _SLICER_FAMILIES else "shows no data"
            )
            found.append(
                _finding(
                    ctx,
                    "empty_visual",
                    "problem",
                    [view.id],
                    f"{ctx.title(view)} {words} with the current filters.",
                    "Check its filters and the slicers on the page.",
                    detail="no_rows",
                )
            )
            continue
        measures = _measure_indexes(row)
        if measures and all(
            isinstance(r, list) and all(i >= len(r) or r[i] is None for i in measures) for r in data
        ):
            found.append(
                _finding(
                    ctx,
                    "empty_visual",
                    "problem",
                    [view.id],
                    f"{ctx.title(view)} shows only blanks with the current filters.",
                    "Check its measures and filters.",
                    detail="blank",
                )
            )
    return found


# --- rules: geometry ------------------------------------------------------------------------------


def _page_size(ctx: _Context) -> tuple[float, float]:
    width = prop_number(ctx.page.data.get("width"))
    height = prop_number(ctx.page.data.get("height"))
    return (
        width if width and width > 0 else 1280.0,
        height if height and height > 0 else 720.0,
    )


def _rule_off_canvas(ctx: _Context) -> list[Finding]:
    found: list[Finding] = []
    page_width, page_height = _page_size(ctx)
    slack = validation.EDGE_TOLERANCE
    for view in ctx.views:
        box = view.box
        outside = {
            "left": -box.x,
            "top": -box.y,
            "right": box.right - page_width,
            "bottom": box.bottom - page_height,
        }
        edges = [edge for edge, amount in outside.items() if amount > slack]
        if not edges:
            continue
        gone = box.x >= page_width or box.y >= page_height or box.right <= 0 or box.bottom <= 0
        width = min(box.width, page_width)
        height = min(box.height, page_height)
        target = {
            "x": min(max(box.x, 0.0), page_width - width),
            "y": min(max(box.y, 0.0), page_height - height),
            "width": width,
            "height": height,
        }
        current = {"x": box.x, "y": box.y, "width": box.width, "height": box.height}
        op: dict[str, Any] = {"op": "set_position", "visual": view.id}
        for key, value in target.items():
            if abs(value - current[key]) > TOLERANCE:
                op[key] = round(value, 2)
        message = (
            f"{ctx.title(view)} is off the page, so nobody sees it."
            if gone
            else f"{ctx.title(view)} is cut off by the {join_names(edges)} "
            f"edge{'s' if len(edges) > 1 else ''} of the page."
        )
        found.append(
            _finding(
                ctx,
                "off_canvas",
                "problem",
                [view.id],
                message,
                "Move it back inside the page.",
                fix=[op] if len(op) > 2 else None,
                amount=max(outside.values()),
            )
        )
    return found


def _rect(ctx: _Context, view: _View) -> tuple[float, float, float, float]:
    found = validation.page_rect(ctx.page, view.id)
    box = view.box
    return found if found is not None else (box.x, box.y, box.width, box.height)


def _rule_overlap(ctx: _Context) -> list[Finding]:
    """The validation's advisory overlap test (a quarter of the smaller visual covered), on
    the visuals a reader sees: text boxes, shapes, pictures and buttons are overlays by nature
    (the validation leaves them out too), and a text-only visual laid over another as its
    caption is a label, not a collision."""
    candidates = [
        v
        for v in ctx.views
        if v.visual_type not in validation.FRAME_TYPES and v.id not in ctx.overlay_ids
    ]
    if len(candidates) > validation.MAX_OVERLAP_VISUALS:
        ctx.notes.append("The page has too many visuals to check them all for overlaps.")
        return []
    run = validation.ValidationRun(ctx.document, None)
    found: list[Finding] = []
    for i, first in enumerate(candidates):
        for second in candidates[i + 1 :]:
            if overlap_area(first.box, second.box) <= 0:
                continue
            before = len(run.findings)
            validation.check_overlap(
                run, ctx.page, [(first.id, _rect(ctx, first)), (second.id, _rect(ctx, second))]
            )
            if len(run.findings) == before:
                continue
            smaller = min(first.box.width * first.box.height, second.box.width * second.box.height)
            share = overlap_area(first.box, second.box) / smaller if smaller > 0 else 1.0
            top, under = (first, second) if first.z >= second.z else (second, first)
            found.append(
                _finding(
                    ctx,
                    "overlap",
                    "problem",
                    [top.id, under.id],
                    f"{ctx.title(top)} covers {round(share * 100)}% of {under.words}.",
                    "Move one of them or make it smaller so both can be read.",
                    amount=share,
                )
            )
    return found


def _padding(fmt: Json) -> tuple[float, float, float, float]:
    """(top, right, bottom, left) inner padding in px."""
    container = fmt.get("container")
    raw = container.get("padding") if isinstance(container, dict) else None
    sides = raw if isinstance(raw, list) and len(raw) == 4 else [None] * 4
    values = [prop_number(s, DEFAULT_PADDING) for s in sides]
    return (
        values[0] or 0.0,
        values[1] or 0.0,
        values[2] or 0.0,
        values[3] or 0.0,
    )


def _shown(token: Any) -> TypeGuard[Json]:
    """A token that is there and switched on."""
    return isinstance(token, dict) and token.get("show") is True


def _size_of(token: Any, key: str = "fontSize") -> float | None:
    return prop_number(token.get(key)) if isinstance(token, dict) else None


def _rule_too_small(ctx: _Context) -> list[Finding]:
    found: list[Finding] = []
    minimum = validation.MIN_SIZE
    for view in ctx.views:
        if not view.is_data:
            continue  # a line shape or an icon is thin on purpose
        box = view.box
        if box.width < minimum or box.height < minimum:
            width, height = default_size(view.visual_type)
            op: dict[str, Any] = {"op": "set_position", "visual": view.id}
            if box.width < minimum:
                op["width"] = width
            if box.height < minimum:
                op["height"] = height
            found.append(
                _finding(
                    ctx,
                    "too_small",
                    "problem",
                    [view.id],
                    f"{ctx.title(view)} is only {show(box.width)} x {show(box.height)} px, "
                    "too small to read.",
                    f"Give it Power BI's usual size for a {type_word(view.visual)}.",
                    detail="minimum",
                    fix=[op],
                    amount=minimum - min(box.width, box.height),
                )
            )
            continue
        if view.family in _CHART_FAMILIES and (
            box.width < CHART_MIN_WIDTH or box.height < CHART_MIN_HEIGHT
        ):
            op = {"op": "set_position", "visual": view.id}
            if box.width < CHART_MIN_WIDTH:
                op["width"] = CHART_MIN_WIDTH
            if box.height < CHART_MIN_HEIGHT:
                op["height"] = CHART_MIN_HEIGHT
            found.append(
                _finding(
                    ctx,
                    "too_small",
                    "suggestion",
                    [view.id],
                    f"{ctx.title(view)} is {show(box.width)} x {show(box.height)} px: too small "
                    "for its title, axes and bars to be read.",
                    f"Make it at least {show(CHART_MIN_WIDTH)} x {show(CHART_MIN_HEIGHT)} px.",
                    detail="chart",
                    fix=[op],
                    amount=max(CHART_MIN_WIDTH - box.width, CHART_MIN_HEIGHT - box.height),
                )
            )
            continue
        value = view.fmt.get("cardValue")
        if view.family in _CARD_FAMILIES and _shown(value):
            size = _size_of(value) or 0.0
            label = view.fmt.get("cardLabel")
            needed = size * LINE_HEIGHT
            if _shown(label):
                needed += (_size_of(label) or 0.0) * LINE_HEIGHT
            top, _, bottom, _ = _padding(view.fmt)
            needed += top + bottom
            if box.height + TOLERANCE < needed:
                height = float(math.ceil(needed))
                found.append(
                    _finding(
                        ctx,
                        "too_small",
                        "suggestion",
                        [view.id],
                        f"{ctx.title(view)} is {show(box.height)} px tall, too short for its "
                        f"{_pt_text(size)} number, which gets cut off.",
                        f"Make it {show(height)} px tall.",
                        detail="card",
                        fix=[{"op": "set_position", "visual": view.id, "height": height}],
                        amount=needed - box.height,
                    )
                )
    return found


# --- rules: alignment and spacing -------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    """A visual as the layout checks see it: a host and the caption laid over it are one
    composite (its box is their union), and a composite is never moved by a one-visual fix."""

    view: _View
    box: Box
    composite: bool


def _union(boxes: Sequence[Box]) -> Box:
    left = min(b.x for b in boxes)
    top = min(b.y for b in boxes)
    right = max(b.right for b in boxes)
    bottom = max(b.bottom for b in boxes)
    return Box(left, top, right - left, bottom - top)


def _slots(ctx: _Context) -> list[_Slot]:
    slots: list[_Slot] = []
    for view in ctx.views:
        if view.id in ctx.overlay_ids or view.family == "textbox":
            continue  # a text box's frame is invisible; a caption moves with its host
        members = [ctx.by_id[o].box for o in ctx.overlays.get(view.id, []) if o in ctx.by_id]
        box = _union([view.box, *members]) if members else view.box
        slots.append(_Slot(view, box, bool(members)))
    return slots


def _span_overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    return min(a1, b1) - max(a0, b0)


def _intersects(box: Box, x0: float, y0: float, x1: float, y1: float) -> bool:
    return min(box.right, x1) - max(box.x, x0) > TOLERANCE and (
        min(box.bottom, y1) - max(box.y, y0) > TOLERANCE
    )


def _neighbours(slots: Sequence[_Slot], across: bool) -> list[tuple[_Slot, _Slot]]:
    """Pairs side by side (``across``: first left of second) or stacked (first above second)
    that share enough of their height (width) and have nothing between them."""
    pairs: list[tuple[_Slot, _Slot]] = []
    for a in slots:
        for b in slots:
            if a is b:
                continue
            A, B = a.box, b.box
            if across:
                if B.x < A.right - TOLERANCE:
                    continue
                shared = _span_overlap(A.y, A.bottom, B.y, B.bottom)
                if shared < NEIGHBOUR_SHARE * min(A.height, B.height):
                    continue
                corridor = (A.right, max(A.y, B.y), B.x, min(A.bottom, B.bottom))
            else:
                if B.y < A.bottom - TOLERANCE:
                    continue
                shared = _span_overlap(A.x, A.right, B.x, B.right)
                if shared < NEIGHBOUR_SHARE * min(A.width, B.width):
                    continue
                corridor = (max(A.x, B.x), A.bottom, min(A.right, B.right), B.y)
            if any(c is not a and c is not b and _intersects(c.box, *corridor) for c in slots):
                continue
            pairs.append((a, b))
    return pairs


def _near(distance: float) -> bool:
    return NEAR_MISS_MIN <= distance <= NEAR_MISS_MAX


def _popularity(slots: Sequence[_Slot], pair: tuple[_Slot, _Slot], value: float, attr: str) -> int:
    """How many other visuals share this edge or size (to half a pixel): the value the page
    already agrees on is the one to keep."""
    return sum(
        1
        for s in slots
        if s is not pair[0] and s is not pair[1] and abs(_edge(s.box, attr) - value) < TOLERANCE
    )


def _edge(box: Box, attr: str) -> float:
    return {
        "x": box.x,
        "y": box.y,
        "right": box.right,
        "bottom": box.bottom,
        "width": box.width,
        "height": box.height,
    }[attr]


def _mover(
    ctx: _Context, slots: Sequence[_Slot], a: _Slot, b: _Slot, attr: str
) -> tuple[_Slot, _Slot]:
    """(the one to change, the one to keep): the visual the edit touched moves; else the one
    whose value fewer visuals share; else the later one in reading order."""
    in_a, in_b = a.view.id in ctx.focus, b.view.id in ctx.focus
    if in_a != in_b:
        return (a, b) if in_a else (b, a)
    pop_a = _popularity(slots, (a, b), _edge(a.box, attr), attr)
    pop_b = _popularity(slots, (a, b), _edge(b.box, attr), attr)
    if pop_a != pop_b:
        return (a, b) if pop_a < pop_b else (b, a)
    return (a, b) if a.view.order > b.view.order else (b, a)


def _rule_near_miss(ctx: _Context, slots: list[_Slot]) -> list[Finding]:
    found: list[Finding] = []
    for across in (True, False):
        lead, far, size = ("y", "bottom", "height") if across else ("x", "right", "width")
        for a, b in _neighbours(slots, across):
            d_lead = abs(_edge(a.box, lead) - _edge(b.box, lead))
            d_far = abs(_edge(a.box, far) - _edge(b.box, far))
            d_size = abs(_edge(a.box, size) - _edge(b.box, size))
            lead_near = _near(d_lead)
            far_near = _near(d_far) and (d_lead < NEAR_MISS_MIN or lead_near)
            if not lead_near and not far_near:
                continue
            if lead_near:
                mover, keep = _mover(ctx, slots, a, b, lead)
            else:
                mover, keep = _mover(ctx, slots, a, b, size)
            op: dict[str, Any] = {"op": "set_position", "visual": mover.view.id}
            where = _where(mover.box, keep.box)
            if lead_near:
                op[lead] = _edge(keep.box, lead)
                if _near(d_size) and far_near:
                    op[size] = _edge(keep.box, size)
                    message = (
                        f"{ctx.title(mover.view)} is {show(d_lead)} px out of line with "
                        f"{keep.view.words} {where} and {show(d_size)} px "
                        f"{_bigger(mover.box, keep.box, size)}."
                    )
                    summary = f"Line it up with {keep.view.words} and give it the same {size}."
                else:
                    message = (
                        f"{ctx.title(mover.view)} is {show(d_lead)} px out of line with "
                        f"{keep.view.words} {where}."
                    )
                    edge_words = "tops" if across else "left edges"
                    summary = f"Move it {show(d_lead)} px so their {edge_words} line up."
            else:
                op[size] = _edge(keep.box, size)
                message = (
                    f"{ctx.title(mover.view)} is {show(d_size)} px "
                    f"{_bigger(mover.box, keep.box, size)} than {keep.view.words} {where}."
                )
                summary = f"Make it {show(_edge(keep.box, size))} px {_dimension(size)}."
            fix: list[dict[str, Any]] | None = [op]
            if mover.composite:
                fix = None
                summary = (
                    f"Move {mover.view.words} together with the text laid over it; "
                    "moving one would split them."
                )
            found.append(
                _finding(
                    ctx,
                    "near_miss",
                    "suggestion",
                    [mover.view.id, keep.view.id],
                    message,
                    summary,
                    detail="row" if across else "column",
                    fix=fix,
                    amount=max(d_lead if lead_near else 0.0, d_far if far_near else 0.0),
                )
            )
    return found


def _where(mover: Box, keep: Box) -> str:
    if keep.right <= mover.x + TOLERANCE or keep.x >= mover.right - TOLERANCE:
        return "beside it"
    return "above it" if keep.y < mover.y else "below it"


def _bigger(mover: Box, keep: Box, size: str) -> str:
    more = _edge(mover, size) > _edge(keep, size)
    if size == "height":
        return "taller" if more else "shorter"
    return "wider" if more else "narrower"


def _dimension(size: str) -> str:
    return "tall" if size == "height" else "wide"


def _chains(slots: Sequence[_Slot], across: bool) -> list[list[_Slot]]:
    """Rows (``across``) or columns of visuals lined up by a shared edge (within the near-miss
    distance), each linked to its nearest neighbour."""
    lead, far = ("y", "bottom") if across else ("x", "right")
    start = "x" if across else "y"
    end = "right" if across else "bottom"
    following: dict[int, _Slot] = {}
    for a, b in _neighbours(slots, across):
        if (
            min(
                abs(_edge(a.box, lead) - _edge(b.box, lead)),
                abs(_edge(a.box, far) - _edge(b.box, far)),
            )
            > NEAR_MISS_MAX
        ):
            continue
        current = following.get(id(a))
        gap = _edge(b.box, start) - _edge(a.box, end)
        if current is None or gap < _edge(current.box, start) - _edge(a.box, end):
            following[id(a)] = b
    targets = {id(b) for b in following.values()}
    chains: list[list[_Slot]] = []
    for slot in slots:
        if id(slot) in targets or id(slot) not in following:
            continue
        chain, seen = [slot], {id(slot)}
        while id(chain[-1]) in following and id(following[id(chain[-1])]) not in seen:
            chain.append(following[id(chain[-1])])
            seen.add(id(chain[-1]))
        if len(chain) >= 3:
            chains.append(chain)
    return chains


def _rule_uneven_gaps(ctx: _Context, slots: list[_Slot]) -> list[Finding]:
    found: list[Finding] = []
    for across in (True, False):
        start, end = ("x", "right") if across else ("y", "bottom")
        for chain in _chains(slots, across):
            gaps = [
                _edge(b.box, start) - _edge(a.box, end)
                for a, b in zip(chain, chain[1:], strict=False)
            ]
            spread = max(gaps) - min(gaps)
            if not NEAR_MISS_MIN <= spread <= UNEVEN_GAP_MAX:
                continue
            ids = [s.view.id for s in chain]
            names = join_names(_distinct_names([s.view for s in chain]))
            fix: list[dict[str, Any]] | None = [
                {
                    "op": "distribute_visuals",
                    "visuals": ids,
                    "axis": "horizontal" if across else "vertical",
                }
            ]
            summary = "Space them evenly; the two at the ends stay where they are."
            if any(s.composite for s in chain):
                fix = None
                summary = "Space them evenly, moving each text laid over a visual with it."
            found.append(
                _finding(
                    ctx,
                    "uneven_gaps",
                    "suggestion",
                    ids,
                    f"The gaps between {names} run from {show(min(gaps))} to {show(max(gaps))} px.",
                    summary,
                    detail="row" if across else "column",
                    fix=fix,
                    amount=spread,
                )
            )
    return found


# --- rules: captions ---------------------------------------------------------------------


def _caption(view: _View) -> tuple[str, str] | None:
    """(the words a reader takes as the visual's name, where they are): a card's caption, a
    slicer's header, else the title (automatic titles included)."""
    fmt = view.fmt
    label = fmt.get("cardLabel")
    if view.family in _CARD_FAMILIES and _shown(label):
        text = str(label.get("text") or "").strip()
        if not text and view.projections:
            text = projection_label(view.projections[0]).strip()
        if text:
            return text, "label"
    slicer = fmt.get("slicer")
    header = slicer.get("header") if isinstance(slicer, dict) else None
    if view.family == "slicer" and _shown(header):
        text = str(header.get("text") or "").strip()
        if not text and view.projections:
            text = projection_label(view.projections[0]).strip()
        if text:
            return text, "header"
    title = fmt.get("title")
    if _shown(title):
        text = str(title.get("text") or "").strip()
        if text:
            return text, "title"
        if title.get("textSource") == "dynamic":
            return DYNAMIC_TITLE, "title"
    return None


# --- rules: data ----------------------------------------------------------------------------------


def _columns(view: _View) -> list[Json]:
    columns = view.row.get("columns") if view.row is not None else None
    return [c for c in columns if isinstance(c, dict)] if isinstance(columns, list) else []


def _rule_number_formats(ctx: _Context) -> list[Finding]:
    seen: dict[str, list[tuple[_View, tuple[str, Any, Any], Json]]] = {}
    for view in ctx.views:
        for column in _columns(view):
            ref = column.get("query_ref")
            if (
                column.get("kind") != "measure"
                or column.get("hidden")
                or column.get("role") in ("Formatting", "Tooltips")
                or not isinstance(ref, str)
            ):
                continue
            signature = (
                str(column.get("format") or ""),
                column.get("displayUnits"),
                column.get("decimals"),
            )
            seen.setdefault(ref, []).append((view, signature, column))
    found: list[Finding] = []
    for ref in sorted(seen):
        shown = seen[ref]
        signatures = Counter(sig for _, sig, _ in shown)
        if len(signatures) < 2:
            continue
        (keep, votes), *rest = signatures.most_common()
        majority = votes >= 2 and not (rest and rest[0][1] == votes)
        label = str(shown[0][2].get("label") or ref)
        odd = [(v, sig) for v, sig, _ in shown if not majority or sig != keep]
        every = [v for v, _, _ in shown]
        called = dict(zip([v.id for v in every], _distinct_names(every), strict=True))
        names = join_names([called[v.id] for v, _ in odd])
        fix: list[dict[str, Any]] | None = None
        if majority and keep[0] and keep[1] is None and keep[2] is None:
            fix = [
                {"op": "set_number_format", "visual": v.id, "field": ref, "format": keep[0]}
                for v, _ in odd
            ]
        found.append(
            _finding(
                ctx,
                "mixed_number_formats",
                "suggestion",
                sorted({v.id for v, _, _ in shown}),
                f"{label} is formatted differently on this page"
                + (
                    f": {names} {'differs' if len(odd) == 1 else 'differ'} from the rest."
                    if majority
                    else f" ({names})."
                ),
                f"Show it as {keep[0]} everywhere." if fix else "Pick one format for it.",
                detail=ref,
                fix=fix,
            )
        )
    return found


def _distinct(view: _View, roles: Iterable[str]) -> tuple[int, bool] | None:
    """Distinct values of the first visible column in one of ``roles``, and whether the rows
    were truncated; None when the visual has no such column or no rows."""
    if view.row is None or "rows" not in view.row:
        return None
    wanted = set(roles)
    index = next(
        (
            i
            for i, c in enumerate(_columns(view))
            if c.get("kind") == "column" and c.get("role") in wanted and not c.get("hidden")
        ),
        None,
    )
    data = view.row.get("rows")
    if index is None or not isinstance(data, list):
        return None
    values = {repr(r[index]) for r in data if isinstance(r, list) and index < len(r)}
    return len(values), bool(view.row.get("truncated"))


def _rule_charts(ctx: _Context) -> list[Finding]:
    found: list[Finding] = []
    for view in ctx.views:
        if view.family not in _CHART_FAMILIES:
            continue
        if view.visual_type in _PIES:
            slices = _distinct(view, ("Category", "Legend", "Series"))
            if slices is not None and (slices[0] > MAX_SLICES or slices[1]):
                count = f"more than {slices[0]}" if slices[1] else str(slices[0])
                found.append(
                    _finding(
                        ctx,
                        "pie_slices",
                        "suggestion",
                        [view.id],
                        f"{ctx.title(view)} has {count} slices; past {MAX_SLICES} they are too "
                        "thin to compare.",
                        "Use a bar chart, or group the small slices into 'Other'.",
                        amount=float(slices[0]),
                    )
                )
    return found


def _rule_too_many(ctx: _Context) -> list[Finding]:
    data = [v for v in ctx.views if v.is_data]
    if len(data) <= MAX_VISUALS:
        return []
    return [
        _finding(
            ctx,
            "too_many_visuals",
            "suggestion",
            [],
            f"Page '{ctx.page.display_name}' has {len(data)} visuals with data; past "
            f"{MAX_VISUALS} it opens slowly and is hard to take in.",
            "Move some of them to another page, or combine cards into one.",
            amount=float(len(data)),
        )
    ]


def _field_key(projection: Json) -> str | None:
    spec = projection.get("field")
    if not isinstance(spec, dict):
        return None
    for kind in ("Column", "Measure", "HierarchyLevel"):
        inner = spec.get(kind)
        if isinstance(inner, dict):
            source = inner.get("Expression")
            ref = source.get("SourceRef") if isinstance(source, dict) else None
            entity = ref.get("Entity") if isinstance(ref, dict) else None
            prop = inner.get("Property") or inner.get("Level")
            if isinstance(entity, str) and isinstance(prop, str):
                return f"{entity}.{prop}"
    ref = projection.get("queryRef")
    return ref if isinstance(ref, str) else None


def _distinct_names(views: Sequence[_View]) -> list[str]:
    """The visuals as a reader names them, with their place added when two read the same
    (two slicers of one field without a header of their own)."""
    words = [v.words for v in views]
    return [
        f"{w} at {show(v.box.x)}, {show(v.box.y)}" if words.count(w) > 1 else w
        for w, v in zip(words, views, strict=True)
    ]


def _rule_same_field_slicers(ctx: _Context) -> list[Finding]:
    """Two slicers on one page over one field: one of them was meant for another field (the
    Agents page has a slicer headed "Country" that filters Channel, next to the Channel
    slicer)."""
    by_field: dict[str, list[_View]] = {}
    for view in ctx.views:
        if view.family not in _SLICER_FAMILIES or not view.projections:
            continue
        key = _field_key(view.projections[0])
        if key is not None:
            by_field.setdefault(key, []).append(view)
    found: list[Finding] = []
    for key in sorted(by_field):
        same = by_field[key]
        if len(same) < 2:
            continue
        field_name = projection_label(same[0].projections[0]).strip() or key.split(".")[-1]
        column = key.split(".", 1)[-1]
        words = {w.casefold() for w in re.findall(r"[A-Za-z]{3,}", f"{field_name} {column}")}
        mislabelled = [
            v
            for v in same
            if (caption := _caption(v)) is not None
            and not words & {w.casefold() for w in re.findall(r"[A-Za-z]{3,}", caption[0])}
        ]
        names = join_names(_distinct_names(same), len(same))
        message = (
            f"{sentence(names)} all filter {field_name}."
            if len(same) > 2
            else (f"{sentence(names)} both filter {field_name}.")
        )
        if mislabelled:
            wrong = mislabelled[0]
            caption = _caption(wrong)
            header = caption[0] if caption else ""
            message = (
                f"{ctx.title(wrong)} filters {field_name}, the same field as "
                f"{join_names(_distinct_names([v for v in same if v is not wrong]))}, "
                "although its "
                f"header says '{header}'."
            )
        found.append(
            _finding(
                ctx,
                "same_field_slicers",
                "problem" if mislabelled else "suggestion",
                [v.id for v in same],
                message,
                "Point the slicer at the field its header names, or remove one of them."
                if mislabelled
                else "Remove one of them, or point it at another field.",
                detail=key,
                amount=float(len(same)),
            )
        )
    return found


# --- the entry points -------------------------------------------------------------------------


def review_page(
    document: PbirDocument,
    page_id: str,
    *,
    payload: Mapping[str, Any] | None = None,
    focus: Iterable[str] | None = None,
) -> Review:
    """Everything a developer would notice on one page (by id, display name or position).

    ``payload`` is the page's render payload (``render_page`` or ``render_formatting_payload``
    of the same head); ``focus`` the visuals an edit touched. Pure: it reads the document and
    the payload and changes neither."""
    page = document.resolve_page(page_id)
    focus_ids = [f for f in (focus or []) if isinstance(f, str)]
    ctx = _build_context(document, page, dict(payload) if payload else None, focus_ids)
    checked: list[str] = []
    skipped: dict[str, str] = {}
    findings: list[Finding] = []

    def run(rules: Sequence[str], found: list[Finding]) -> None:
        checked.extend(r for r in rules if r not in checked)
        findings.extend(found)

    if ctx.has_payload:
        run(("visual_error", "needs_power_bi"), _rule_errors(ctx))
        run(("mixed_number_formats",), _rule_number_formats(ctx))
    else:
        for rule in _PAYLOAD_RULES:
            skipped[rule] = "Needs the page as Studio draws it (open the page first)."
    if ctx.has_rows:
        run(("empty_visual",), _rule_empty(ctx))
    run(("off_canvas",), _rule_off_canvas(ctx))
    run(("overlap",), _rule_overlap(ctx))
    run(("too_small",), _rule_too_small(ctx))
    run(("same_field_slicers",), _rule_same_field_slicers(ctx))
    slots = _slots(ctx)
    if len(slots) <= MAX_LAYOUT_VISUALS:
        run(("near_miss",), _rule_near_miss(ctx, slots))
        run(("uneven_gaps",), _rule_uneven_gaps(ctx, slots))
    else:
        for rule in ("near_miss", "uneven_gaps"):
            skipped[rule] = f"The page has more than {MAX_LAYOUT_VISUALS} visuals."
    run(("too_many_visuals",), _rule_too_many(ctx))
    if ctx.has_rows:
        run(("pie_slices",), _rule_charts(ctx))
    else:
        for rule in _ROW_RULES:
            if rule not in checked:
                skipped[rule] = "Needs the page's data (open the page first)."
    order = {v.id: v.order for v in ctx.views}

    def sort_key(finding: Finding) -> tuple[int, int, int, int, str]:
        touched = 0 if any(v in ctx.focus for v in finding.visuals) else 1
        first = min((order.get(v, 10_000) for v in finding.visuals), default=-1)
        return (
            touched,
            0 if finding.severity == "problem" else 1,
            _ORDER[finding.rule],
            first,
            finding.key,
        )

    findings.sort(key=sort_key)
    return Review(
        page=page.name,
        page_name=page.display_name,
        findings=findings,
        checked=[r for r in RULES if r in checked],
        skipped={r: skipped[r] for r in RULES if r in skipped},
        notes=list(dict.fromkeys(ctx.notes)),
        focus=focus_ids,
    )


def review_edit(before: Review, after: Review) -> list[Finding]:
    """The findings of ``after`` that ``before`` did not have, or that the edit made worse
    (by at least the rule's step in ``_WORSE``), in ``after``'s order. Old issues are left to
    an explicit "review this page"."""
    known = {f.key: f for f in before.findings}
    fresh: list[Finding] = []
    for finding in after.findings:
        old = known.get(finding.key)
        if old is None or finding.amount - old.amount >= _WORSE.get(finding.rule, math.inf):
            fresh.append(finding)
    return fresh


def describe_findings(findings: Sequence[Finding], limit: int = 3) -> str:
    """The findings as a short plain paragraph for a chat reply or an edit receipt."""
    if not findings:
        return ""
    shown = [f.message for f in findings[:limit]]
    more = len(findings) - len(shown)
    text = " ".join(shown)
    if more:
        text += f" ({more} more thing{'s' if more > 1 else ''} to look at.)"
    return text


__all__ = [
    "MAX_SLICES",
    "MAX_VISUALS",
    "NEAR_MISS_MAX",
    "NEAR_MISS_MIN",
    "RULES",
    "Finding",
    "Review",
    "describe_findings",
    "review_edit",
    "review_page",
]
