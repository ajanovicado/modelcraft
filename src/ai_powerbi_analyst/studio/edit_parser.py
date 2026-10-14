"""Reads one clause of an edit request: undo, pages, measures, new visuals, and (through
edit_visual) everything about existing visuals.

``Parser.clause`` returns a Clause, or None when it cannot read an edit-looking clause; the
caller then hands that clause to the bounded 4B fallback. Nothing here calls a model.

Styling (colors, fonts, titles, data labels, legends, axes, a visual's box, page backgrounds,
the theme) is refused: the capability matrix's styling entries catch most of it before any
recognizer runs (``detect_capability``), and edit_visual's ``styling_clause`` refuses what is
left once the recognizers that use the same words for something else (fields on the legend, a
hidden visual) have had their turn. No recognizer here produces a styling operation.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from . import edit_ops as ops
from .edit_capabilities import (
    CAPABILITY_MATRIX,
    detect_capability,
    is_styling,
    reads_like_styling,
    styling_gives_way,
)
from .edit_context import (
    EditContext,
    MeasureEntry,
    PageEntry,
    VisualEntry,
    change_kinds,
    type_name,
)
from .edit_filters import filter_clause, looks_like_filter
from .edit_frame import Forced, Frame, capability
from .edit_objects import PLACE_WORDS, SPOT_NOT_HONORED, add_object_clause, explicit_box
from .edit_targets import (
    Resolution,
    find_page,
    find_pages_by_name,
    find_types,
    match_names,
    resolve_visuals,
)
from .edit_text import (
    CLEANUP_AFTER_DELETE,
    KEEP_IN_PLACE,
    Amount,
    find_amounts,
    find_colors,
    new_measure_calculation,
    plain_number,
    quoted,
    scaled,
    short_number,
    tokens,
)
from .edit_types import Choice, Clause, ParseKind, Target, WhatIf
from .edit_visual import (
    explicit_text_span,
    find_new_type,
    normalise_type,
    styling_signal,
    visual_clause,
)

Op = dict[str, Any]

# --- undo family -------------------------------------------------------------------------------

_UNDO = re.compile(
    r"^(?:please\s+)?(?:undo|revert|roll\s*back|take\s+(?:that|it|this)\s+back|go\s+back|"
    r"cancel\s+(?:that|it|this|the\s+last)|put\s+(?:it|that|this|them)\s+back|"
    r"change\s+it\s+back|switch\s+it\s+back|reverse\s+that)\b",
    re.I,
)
_REDO = re.compile(
    r"^(?:please\s+)?(?:redo|do\s+(?:that|it)\s+again|reapply|bring\s+(?:it|that)\s+back)\b", re.I
)
_RESTORE = re.compile(
    r"\b(?:(?:restore|revert|reset|go\s+back|put\s+(?:everything|it\s+all)\s+back|change\s+everything\s+back)"
    r"\s+(?:everything\s+|it\s+all\s+|the\s+(?:report|dashboard|file|whole\s+thing)\s+)?(?:back\s+)?to\s+"
    r"(?:the\s+|its\s+)?(?:original|start|beginning|initial|first|as\s+imported)|"
    r"(?:start|begin)\s+(?:over|again|from\s+scratch)|undo\s+(?:everything|all(?:\s+(?:of\s+)?(?:the|my|your))?\s*"
    r"(?:changes|edits)?)|discard\s+(?:all\s+)?(?:the\s+|my\s+|your\s+)?(?:changes|edits)|"
    r"restore\s+(?:the\s+)?original|(?:revert|reset|discard|undo)\s+(?:absolutely\s+)?"
    r"(?:everything|it\s+all|all\s+of\s+it|all\s+(?:of\s+)?(?:the\s+|my\s+|your\s+)?"
    r"(?:changes|edits)))\b",
    re.I,
)
_WHAT_CHANGED = re.compile(
    r"\b(?:what\s+(?:did\s+you|have\s+you|was|has)\s+(?:change[d]?|do|done|edit(?:ed)?)|what\s+changed|"
    r"what(?:'s|\s+is)\s+(?:been\s+)?changed|(?:show|list|tell)\s+(?:me\s+)?(?:all\s+)?(?:the\s+|my\s+)?"
    r"(?:changes|edits|history)|what\s+edits|which\s+changes|recap\s+(?:the\s+)?changes|"
    r"summari[sz]e\s+(?:the\s+)?changes|(?:summary|list|recap|overview)\s+of\s+(?:what\s+you\s+"
    r"(?:changed|did)|(?:the\s+|all\s+)?(?:changes|edits))|what\s+you\s+(?:changed|did))\b",
    re.I,
)
_UNDO_KIND = re.compile(
    r"\bundo\s+(?:the\s+|that\s+|my\s+|your\s+|this\s+|last\s+|latest\s+)*(?P<what>colou?r|font|"
    r"title|heading|size|resiz|layout|label|legend|background|border|mov|position|delet|remov|"
    r"renam|hid|page|measure|filter)\w*(?:\s+change|\s+edit)?\b",
    re.I,
)
# the word a person says -> the kinds of change (edit_context.change_kinds) that it can mean
_UNDO_MEANS = {
    "color": {"color"},
    "colour": {"color"},
    "font": {"font"},
    "title": {"title", "rename"},
    "heading": {"title", "rename"},
    "size": {"size", "font"},
    "resiz": {"size"},
    "layout": {"layout", "size"},
    "label": {"label"},
    "legend": {"legend"},
    "background": {"background"},
    "border": {"border"},
    "mov": {"layout"},
    "position": {"layout"},
    "delet": {"delete"},
    "remov": {"delete"},
    "renam": {"rename"},
    "hid": {"hide"},
    "page": {"page"},
    "measure": {"measure"},
    "filter": {"filter"},
}
_UNDO_LABEL = {
    "color": "a color change",
    "colour": "a color change",
    "font": "a font change",
    "title": "a title change",
    "heading": "a title change",
    "size": "a size change",
    "resiz": "a size change",
    "layout": "a layout change",
    "label": "a label change",
    "legend": "a legend change",
    "background": "a background change",
    "border": "a border change",
    "mov": "a move",
    "position": "a position change",
    "delet": "a deletion",
    "remov": "a deletion",
    "renam": "a rename",
    "hid": "a hide",
    "page": "a page change",
    "measure": "a measure change",
    "filter": "a filter change",
}
_UNDO_MANY = re.compile(
    r"\bundo\s+(?:the\s+)?(?:last|previous|latest|past)\s+(?:two|three|four|five|\d+|few|couple"
    r"(?:\s+of)?|several|some)\s+(?:changes|edits|things|steps)\b|\bundo\s+(?:it\s+|that\s+)?"
    r"(?:twice|three\s+times|\d+\s+times)\b",
    re.I,
)
_META_START = re.compile(
    r"^(?:(?:please|then|and|also|now|ok(?:ay)?|so|next|after\s+that|finally)\b[,.]?\s*)+", re.I
)


def _undo_choices() -> tuple[Choice, ...]:
    return (
        Choice(label="Undo the latest change", fill={"kind": "undo"}),
        Choice(label="Show what changed", fill={"kind": "what_changed"}),
        Choice(label="Restore the original report", fill={"kind": "restore"}),
    )


def meta_clause(sentence: str, context: EditContext) -> Clause | None:
    """Undo, redo, restore and "what did you change": never a model call, never an edit."""
    text = sentence.strip()
    body = _META_START.sub("", text).strip()
    last = context.last_edit
    if _WHAT_CHANGED.search(text):
        return Clause(text, ParseKind.WHAT_CHANGED, summary="Here is what changed in this report.")
    if _RESTORE.search(text):
        return Clause(
            text,
            ParseKind.RESTORE,
            summary="Restore the report to the file you imported, undoing every change.",
        )
    if _UNDO_MANY.search(body) and _UNDO.match(body):
        return Clause(
            text,
            ParseKind.CLARIFY,
            question="I undo one change at a time. Undo the latest change, or go back to the "
            "original report?",
            choices=_undo_choices(),
        )
    kind = _UNDO_KIND.search(body)
    if kind and _UNDO.match(body):
        what = kind.group("what").casefold()
        if last is None:
            return Clause(
                text,
                ParseKind.CLARIFY,
                question="There is nothing to undo yet: no change has been made in this report.",
            )
        made = change_kinds(last.ops)
        wanted = _UNDO_MEANS.get(what, set())
        label = _UNDO_LABEL.get(what, what)
        if wanted and not made & wanted:
            return Clause(
                text,
                ParseKind.CLARIFY,
                question=(
                    f"My latest change was not {label}"
                    + (f" ({last.summary.rstrip('.')})" if last.summary else "")
                    + ". Undo it anyway, or see everything that changed?"
                ),
                choices=_undo_choices()[:2],
            )
        return Clause(
            text,
            ParseKind.UNDO,
            summary=f"Undo {label}" + (f": {last.summary}" if last.summary else "."),
        )
    if _REDO.match(body):
        return Clause(text, ParseKind.REDO, summary="Redo the change that was undone.")
    if _UNDO.match(body):
        summary = "Undo the latest change" + (f": {last.summary}" if last and last.summary else ".")
        return Clause(text, ParseKind.UNDO, summary=summary)
    return None


# --- vague style requests ---------------------------------------------------------------------

_VAGUE = re.compile(
    r"\b(?:look(?:s)?\s+(?:nicer|better|good|great|professional|modern|cleaner|polished|sharper|"
    r"sleek|slick|fancy|more\s+\w+)|(?:more|less)\s+(?:professional|modern|polished|clean|"
    r"colou?rful|busy|cluttered|appealing|attractive|elegant|minimal\w*)|nicer|prettier|"
    r"tidy(?:\s+(?:it|this|things))?(?:\s+up)?|clean\s+(?:it|this|things|the\s+\w+)\s+up|"
    r"clean\s+up|polish\w*|spruce\s+\w*\s*up|improve\s+(?:the\s+|its\s+)?(?:look|design|style|"
    r"appearance|formatting|layout|colou?rs?)|redesign|restyle|moderni[sz]e|make\s+it\s+pop|"
    r"(?:use|apply|set|add)\s+(?:a\s+|our\s+|my\s+|the\s+)?brand\w*|dark\s+(?:mode|theme)|"
    r"better\s+colou?rs?|pretty|beautif\w+|professional|stylish|slick)\b",
    re.I,
)


def vague_clause(f: Frame) -> Clause | None:
    """ "Make it look nicer", "use a brand palette": how the report looks, which Studio does not
    change. The answer is the styling refusal; no change is offered in its place."""
    if not _VAGUE.search(f.masked):
        return None
    return f.unsupported(capability("styling"))


# --- pages ---------------------------------------------------------------------------------------

_VISUAL_WORD = re.compile(
    r"\b(?:card|chart|graph|slicer|table|matrix|visual|kpi|gauge|map|image|button|shape|text\s?box|bar|column|line)s?\b",
    re.I,
)
_PAGE_WORD = r"(?:page|tab)s?"


def _verb_page(low: str, verbs: str) -> bool:
    """The page (not a visual) is what the verb acts on: "delete the Details page"."""
    for match in re.finditer(
        rf"\b(?:{verbs})\s+(?:the\s+|this\s+|that\s+|my\s+|a\s+|an\s+)?((?:[\w'-]+\s+){{0,3}}?){_PAGE_WORD}\b",
        low,
    ):
        if not _VISUAL_WORD.search(match.group(1)):
            return True
    return False


def _unique_page_name(context: EditContext, base: str) -> str:
    taken = {p.name.casefold() for p in context.pages}
    if base.casefold() not in taken:
        return base
    number = 2
    while f"{base} {number}".casefold() in taken:
        number += 1
    return f"{base} {number}"


def _value_after(text: str, words: str) -> str | None:
    """Quoted text, or what follows one of ``words`` up to the end of the clause."""
    match = re.search(rf"\b(?:{words})\s+(?:to\s+|as\s+)?[\"'](?P<q>[^\"']+)[\"']", text, re.I)
    if match:
        return match.group("q").strip()
    match = re.search(rf"\b(?:{words})\s+(?:to\s+|as\s+)?(?P<v>[^,;]+?)\s*[.!?]?$", text, re.I)
    if match:
        value = match.group("v").strip()
        value = re.sub(r"^(?:the\s+)?(?:name|title)\s+", "", value, flags=re.I)
        return value.strip("\"' ") or None
    return None


_SIZES = {
    "16:9": (1280.0, 720.0),
    "widescreen": (1280.0, 720.0),
    "hd": (1280.0, 720.0),
    "4:3": (960.0, 720.0),
    "letter": (816.0, 1056.0),
    "a4": (794.0, 1123.0),
    "tooltip": (320.0, 240.0),
    "square": (720.0, 720.0),
}


def _active_page(context: EditContext) -> PageEntry | None:
    if context.active_page:
        return context.page(context.active_page)
    if len(context.pages) == 1:
        return context.pages[0]
    return None


_PAGE_DESCRIBER_WORDS = (
    "this that these those current active first second third fourth fifth sixth seventh "
    "eighth ninth tenth last next previous prior new blank empty extra another other whole "
    "entire same selected open visible hidden main only single one report dashboard all every "
    "each canvas page tab background wallpaper"
)
_PAGE_DESCRIBERS = frozenset(_PAGE_DESCRIBER_WORDS.split())


def _unknown_page_name(f: Frame) -> str | None:
    """The name in "delete the Zebra page" when no page has it: the request meant another page,
    and the active one is not a guess to act on (a delete would remove the wrong page)."""
    pattern = rf"\b(?:the|a|my)\s+(?P<name>[\w'&-]+(?:\s+[\w'&-]+){{0,2}}?)\s+{_PAGE_WORD}\b"
    for found in re.finditer(pattern, f.text, re.I):
        words = found.group("name").split()
        if all(w.casefold() not in _PAGE_DESCRIBERS for w in words) and not any(
            w.isdigit() for w in words
        ):
            return found.group("name")
    return None


def _page_or_ask(f: Frame, verb_text: str = "") -> PageEntry | Clause:
    if f.res.page is None:
        unknown = _unknown_page_name(f)
        if unknown is not None:
            names = ", ".join(f"'{p.name}'" for p in f.context.pages[:8])
            return f.clarify(
                f"I could not find a page called '{unknown}'. Which page? {names}.",
                [
                    Choice(label=p.name, fill={"append": f"page {p.index + 1}"})
                    for p in f.context.pages[:8]
                ],
            )
    page = f.res.page or _active_page(f.context)
    number = re.search(r"\bpage\s+(?:number\s+)?(\d{1,3})\b(?!\s*:\s*\d)", f.low)
    ordinal = re.search(
        r"\b(second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)\s+page\b", f.low
    )
    if (number or ordinal) and f.res.page is None:
        count = len(f.context.pages)
        plural = "s" if count != 1 else ""
        which = number.group(1) if number else ordinal.group(1) if ordinal else ""
        return f.not_available(
            f"There is no {'page ' + which if number else which + ' page'}: this report has "
            f"{count} page{plural}."
        )
    if page is not None:
        return page
    choices = [
        Choice(label=p.name, fill={"append": f"page {p.index + 1}"}) for p in f.context.pages[:8]
    ]
    return f.clarify(
        "Which page? " + ", ".join(f"'{p.name}'" for p in f.context.pages[:8]) + ".", choices
    )


_ADD_PAGE = re.compile(
    rf"\b(?:(?:add|create|insert)\s+(?:me\s+)?(?:(?:a|an|another|one\s+more)\s+)?"
    rf"|(?:make|want|need)\s+(?:me\s+)?(?:a|an|another|one\s+more)\s+)"
    rf"(?:(?:new|blank|empty|extra|second|third)\s+)*(?:report\s+)?{_PAGE_WORD}\b",
    re.I,
)
_DUPLICATE_PAGE = re.compile(
    rf"\b(?:duplicate|clone|copy)\s+(?:the\s+|this\s+|that\s+|my\s+)?(?:[\w'-]+\s+){{0,3}}?"
    rf"{_PAGE_WORD}\b|\bmake\s+a\s+cop(?:y|ies)\s+of\s+(?:the\s+|this\s+)?(?:[\w'-]+\s+){{0,3}}?"
    rf"{_PAGE_WORD}\b",
    re.I,
)
_RENAME_PAGE = re.compile(
    rf"\b(?:rename|retitle)\b.*\b{_PAGE_WORD}\b|\b(?:call|name|title)\s+(?:the\s+|this\s+|that\s+)?"
    rf"(?:[\w'-]+\s+){{0,3}}?{_PAGE_WORD}\b|\b{_PAGE_WORD}\s+(?:name|title)\s+(?:to|as)\b|"
    rf"\bchange\s+the\s+(?:name|title)\s+of\s+(?:the\s+|this\s+)?(?:[\w'-]+\s+){{0,3}}?{_PAGE_WORD}\b",
    re.I,
)
_MOVE_PAGE = re.compile(
    rf"\b(?:move|put|send|reorder|drag|shift)\b.*\b{_PAGE_WORD}\b.*"
    r"\b(?:first|last|front|start|end|beginning|before|after|position|spot|\d+)\b",
    re.I,
)
_MAKE_FIRST = re.compile(
    rf"\b(?:make|put|set)\s+(?:the\s+)?(?P<page>(?:[\w'-]+\s+){{0,4}}?{_PAGE_WORD}(?:\s+\d+)?)\s+"
    r"(?:the\s+)?(?P<where>first|last)\b",
    re.I,
)


def _page_by_name(text: str, context: EditContext) -> PageEntry | None:
    """A page the text names by its own name, with no "page" word ("move Forecast to the front")."""
    words = tokens(text)
    best: tuple[int, int, PageEntry] | None = None
    for page in context.pages:
        wanted = tokens(page.name)
        if len(page.name.strip()) < 3 or not wanted or page.name.strip().isdigit():
            continue
        starts = range(len(words) - len(wanted) + 1)
        at = next((i for i in starts if words[i : i + len(wanted)] == wanted), None)
        # the page named first is the one the verb acts on ("move Forecast before Product Mix")
        if at is not None and (best is None or (at, -len(wanted)) < (best[0], best[1])):
            best = (at, -len(wanted), page)
    return best[2] if best else None


def _page_verbs_only(f: Frame) -> bool:
    """A page named without the word "page": only for verbs that can only mean a page."""
    if _VISUAL_WORD.search(f.low) or _page_by_name(f.text, f.context) is None:
        return False
    page = _page_by_name(f.text, f.context)
    assert page is not None
    caption_hit = any(
        m.tier <= 2 for m in match_names(f.text, [v for v in f.context.visuals if not v.is_group])
    )
    return not caption_hit and bool(
        re.search(
            r"\b(?:move|send|put|reorder|hide|unhide|duplicate|copy|clone|rename|delete|remove)\b",
            f.low,
        )
    )


def _named_page_text(f: Frame) -> str:
    """The clause with the page's own name turned into "page N", so one reader handles both."""
    page = _page_by_name(f.text, f.context)
    if page is None:
        return f.text
    pattern = r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in page.name.split()) + r"(?![\w-])"
    return re.sub(pattern, f"page {page.index + 1}", f.text, count=1, flags=re.I)


_MANY_PAGES = re.compile(
    r"\b(?:delete|remove|drop|get\s+rid\s+of|trash|erase|ditch|scrap)\s+"
    r"(?:(?:all|every|each)\s+(?:of\s+)?(?:the\s+|my\s+)?(?:other\s+)?|(?:the\s+)?(?:other|remaining)\s+)"
    rf"(?:[\w'-]+\s+){{0,2}}?{_PAGE_WORD}\b(?P<tail>.*)$",
    re.I,
)
_KEEP = re.compile(
    r"\b(?:but|except|besides|other\s+than|apart\s+from|excluding|leaving|keeping|keep)\s+(?P<keep>.+)$",
    re.I,
)


def _find_kept(text: str, ctx: EditContext) -> PageEntry | None:
    """The page a "keep" phrase names: "the first", "page 2", "Details", "the Details page"."""
    piece = text.strip(" .!?")
    for attempt in (piece, f"{piece} page"):
        page, _ = find_page(attempt, ctx)
        if page is not None:
            return page
    return _page_by_name(piece, ctx)


def _delete_many_pages(f: Frame) -> Clause | None:
    """ "delete every page but the first": all the other pages go, after a yes; "delete all the
    pages" asks which one stays, because a report keeps at least one."""
    found = _MANY_PAGES.search(f.text)
    if found is None:
        return None
    ctx = f.context
    keep_match = _KEEP.search(found.group("tail")) or _KEEP.search(f.text)
    if keep_match is None:
        choices = [
            Choice(label=f"Keep '{p.name}'", fill={"append": f"but {p.index + 1}"})
            for p in ctx.pages[:8]
        ]
        return f.clarify(
            "A report keeps at least one page. Which page should stay? "
            + ", ".join(f"'{p.name}'" for p in ctx.pages[:8])
            + ".",
            choices,
        )
    kept: list[PageEntry] = []
    for piece in re.split(r"\s*(?:,|&|\band\b|\bor\b)\s*", keep_match.group("keep")):
        if not piece.strip():
            continue
        page = _find_kept(re.sub(r"^\s*(?:the\s+)?", "", piece), ctx) or _find_kept(piece, ctx)
        if page is None and re.fullmatch(r"\s*(?:the\s+)?\d{1,3}\s*", piece):
            page = _find_kept(f"page {piece.strip().removeprefix('the ')}", ctx)
        if page is None:
            return f.clarify(
                f"I could not tell which page to keep ('{piece.strip()}'). Name it, for example "
                '"delete every page but the first".'
            )
        kept.append(page)
    doomed = [p for p in ctx.pages if p.id not in {k.id for k in kept}]
    if not doomed:
        return f.not_available(
            "Every page is one you asked to keep, so there is nothing to delete."
        )
    names = ", ".join(f"'{p.name}'" for p in doomed[:6]) + (
        f" and {len(doomed) - 6} more" if len(doomed) > 6 else ""
    )
    kept_names = " and ".join(f"'{p.name}'" for p in kept)
    return f.clarify(
        f"That deletes {len(doomed)} page{'s' if len(doomed) != 1 else ''} ({names}) and keeps "
        f"{kept_names}. Go ahead?",
        [
            Choice(
                label=f"Yes, delete {len(doomed)} page{'s' if len(doomed) != 1 else ''}",
                ops=tuple({"op": "delete_page", "page": p.id} for p in doomed),
                summary=f"Deleted {len(doomed)} page{'s' if len(doomed) != 1 else ''} ({names}).",
            )
        ],
    )


def page_clause(f: Frame) -> Clause | None:
    low, ctx = f.low, f.context
    if not re.search(rf"\b{_PAGE_WORD}\b|\bwallpaper\b", low):
        if not _page_verbs_only(f):
            return None
        rewritten = _named_page_text(f)
        f = replace(f, text=rewritten, low=rewritten.casefold(), masked=rewritten.casefold())
        f.res = resolve_visuals(rewritten, ctx, carry=f.carry, forced=f.forced.visuals)
        low = f.low
    if re.search(rf"\b{_PAGE_WORD}\s+#?(?:[-−]\s*\d+|0+)(?![\d.])", low):
        return f.not_available("There is no such page: pages are numbered from 1.")
    many = _delete_many_pages(f)
    if many is not None:
        return many
    if (
        _ADD_PAGE.search(low)
        and not re.search(r"\b(?:rename|duplicate|delete|hide)\b", low)
        and not _VISUAL_WORD.search(_ADD_PAGE.sub("", low))
    ):
        return _add_page(f)
    if _DUPLICATE_PAGE.search(low):
        page = _page_or_ask(f)
        if isinstance(page, Clause):
            return page
        name = _value_after(f.text, "called|named|as")
        made: Op = {"op": "duplicate_page", "page": page.id}
        if name:
            made["name"] = name
        label = f" as '{name}'" if name else ""
        return _page_result(f, [made], f"Duplicated page '{page.name}'{label}.", page)
    if _verb_page(low, "delete|remove|drop|get\\s+rid\\s+of|trash"):
        named_pages = find_pages_by_name(f.text, ctx)
        if len(named_pages) > 1 and len(named_pages) < len(ctx.pages):
            # "delete the Forecast page and the Product Mix page": both, and only after a yes
            cleanup = bool(CLEANUP_AFTER_DELETE.search(re.split(rf"\b{_PAGE_WORD}\b", low)[-1]))
            names = " and ".join(f"'{p.name}'" for p in named_pages)
            extra: Op = {"cleanup_references": True} if cleanup else {}
            return f.clarify(
                f"That deletes {len(named_pages)} pages ({names}). Go ahead?",
                [
                    Choice(
                        label=f"Yes, delete {len(named_pages)} pages",
                        ops=tuple(
                            {"op": "delete_page", "page": p.id, **extra} for p in named_pages
                        ),
                        summary=f"Deleted {len(named_pages)} pages ({names}).",
                    )
                ],
            )
        page = _page_or_ask(f)
        if isinstance(page, Clause):
            return page
        if len(ctx.pages) == 1:
            return f.not_available(
                "A report needs at least one page, so the only page cannot be deleted."
            )
        if CLEANUP_AFTER_DELETE.search(re.split(rf"\b{_PAGE_WORD}\b", low, maxsplit=1)[-1]):
            # the buttons and bookmarks that pointed at the page go with it
            return _page_result(
                f,
                [{"op": "delete_page", "page": page.id, "cleanup_references": True}],
                f"Deleted page '{page.name}' and the buttons and bookmarks that pointed to it.",
                page,
            )
        return _page_result(
            f, [{"op": "delete_page", "page": page.id}], f"Deleted page '{page.name}'.", page
        )
    # A visual word only counts before the new name: "change the name of page 1 to Agent KPIs"
    # renames the page (it once set the title of every KPI card instead).
    said = re.split(r"\b(?:to|as|called|named)\b", low, maxsplit=1)[0]
    if _RENAME_PAGE.search(low) and not _VISUAL_WORD.search(re.sub(rf"\b{_PAGE_WORD}\b", "", said)):
        if f.res.page is None:
            # "rename the Forecast page to Product Mix" names two pages: the one before "to" is
            # the page, the one after it is only the new name (which may be taken)
            head = re.split(r"\b(?:to|as|called|named)\b", f.text, maxsplit=1)[0]
            before = find_pages_by_name(head, ctx)
            if len(before) == 1:
                f = replace(f, res=replace(f.res, page=before[0]))
        page = _page_or_ask(f)
        if isinstance(page, Clause):
            return page
        name = _value_after(f.text, "to|as|called|named")
        if not name:
            # "call the page Agent Performance": the name follows the word page
            direct = re.search(
                rf"\b(?:call|name|title)\s+(?:the\s+|this\s+|that\s+)?(?:[\w'-]+\s+){{0,2}}?"
                rf"{_PAGE_WORD}\s+(?P<name>[^,;]+?)\s*[.!?]?$",
                f.text,
                re.I,
            )
            name = direct.group("name").strip("\"' ") if direct else None
        if not name:
            return f.clarify(f"What should page '{page.name}' be called?")
        if any(p.name.casefold() == name.casefold() and p.id != page.id for p in ctx.pages):
            return f.not_available(f"There is already a page named '{name}'.")
        return _page_result(
            f,
            [{"op": "rename_page", "page": page.id, "name": name}],
            f"Renamed page '{page.name}' to '{name}'.",
            page,
        )
    if _verb_page(low, "hide|unhide|show|reveal"):
        page = _page_or_ask(f)
        if isinstance(page, Clause):
            return page
        hidden = bool(re.search(r"\bhide\b", low)) and not re.search(r"\bunhide\b", low)
        verb = "Hid" if hidden else "Showed"
        return _page_result(
            f,
            [{"op": "set_page_hidden", "page": page.id, "hidden": hidden}],
            f"{verb} page '{page.name}'.",
            page,
        )
    swap = _MAKE_FIRST.search(low)
    if swap:
        source, _ = find_page(swap.group("page"), ctx)
        if source is None:
            return f.not_available(
                f"That page does not exist: this report has {len(ctx.pages)} page"
                f"{'s' if len(ctx.pages) != 1 else ''}."
            )
        index = 0 if swap.group("where") == "first" else len(ctx.pages) - 1
        return _page_result(
            f,
            [{"op": "move_page", "page": source.id, "index": index}],
            f"Moved page '{source.name}' to position {index + 1}.",
            source,
        )
    if _MOVE_PAGE.search(low):
        page = _page_or_ask(f)
        if isinstance(page, Clause):
            return page
        position = _move_index(f, page)
        if position is None:
            return f.clarify(
                f'Where should page \'{page.name}\' go? For example "first", "last" or '
                '"after page 2".'
            )
        return _page_result(
            f,
            [{"op": "move_page", "page": page.id, "index": position}],
            f"Moved page '{page.name}' to position {position + 1}.",
            page,
        )
    size = _page_size(f)
    if size is not None:
        return size
    if re.search(r"\bbackground\b|\bwallpaper\b", low):
        # a page's background or wallpaper is styling
        return f.unsupported(capability("styling"))
    return None


def _add_page(f: Frame) -> Clause:
    ctx, low = f.context, f.low
    name = _value_after(f.text, "called|named|titled|name\\s+it|call\\s+it")
    if name:
        name = re.split(r"\s+(?:after|before|at|as\s+the|to\s+the)\s+", name, maxsplit=1)[0].strip()
    if not name:
        name = _unique_page_name(ctx, f"Page {len(ctx.pages) + 1}")
    elif any(p.name.casefold() == name.casefold() for p in ctx.pages):
        return f.not_available(f"There is already a page named '{name}'.")
    reference = _active_page(ctx) or (ctx.pages[-1] if ctx.pages else None)
    anchor = reference
    after = re.search(r"\bafter\b\s+(?:the\s+)?(?P<page>.+)$", f.text, re.I)
    if after:
        found, _ = find_page(after.group("page"), ctx)
        anchor = found or reference
    elif re.search(r"\b(?:at\s+the\s+end|last)\b", low) and ctx.pages:
        anchor = ctx.pages[-1]
    made: Op = {"op": "add_page", "name": name}
    if reference is not None:
        made["width"], made["height"] = reference.width, reference.height
    if anchor is not None:
        made["after"] = anchor.id
    return _page_result(f, [made], f"Added page '{name}'.", None)


def _page_result(f: Frame, made: list[Op], summary: str, page: PageEntry | None) -> Clause:
    target = Target(
        (), page.id if page else None, f"page '{page.name}'" if page else "the report", "page"
    )
    return Clause(f.text, ParseKind.APPLY, ops=tuple(made), summary=summary, targets=(target,))


def _move_index(f: Frame, page: PageEntry) -> int | None:
    low, ctx = f.low, f.context
    others = [p for p in ctx.pages if p.id != page.id]
    if re.search(r"\b(?:first|front|start|beginning)\b", low):
        return 0
    if re.search(r"\b(?:last|end)\b", low):
        return len(ctx.pages) - 1
    ref = re.search(r"\b(after|before)\s+(?:the\s+)?(?:page\s+)?(.+)$", f.text, re.I)
    if ref:
        target, _ = find_page(
            ref.group(2) if re.search(r"page", ref.group(0), re.I) else f"page {ref.group(2)}", ctx
        )
        if target is None:
            target, _ = find_page(ref.group(2), ctx)
        if target is not None and target.id != page.id:
            order = [p.id for p in others]
            position = order.index(target.id)
            return position + (1 if ref.group(1).casefold() == "after" else 0)
    number = re.search(r"\b(?:position|spot|number|to)\s+(\d{1,3})\b", low)
    if number:
        return max(0, min(int(number.group(1)) - 1, len(ctx.pages) - 1))
    return None


_KEEP_VISUALS = KEEP_IN_PLACE


def _page_size(f: Frame) -> Clause | None:
    low = f.low
    dims = re.search(
        r"(?<![\d.])(\d{3,6}(?:\.\d+)?)\s*(?:px|pixels?)?\s*(?:x|×|by)\s*(\d{3,6}(?:\.\d+)?)(?![\d.])",
        low,
    )
    named = next(
        (v for k, v in _SIZES.items() if re.search(rf"(?<![\w:]){re.escape(k)}(?![\w:])", low)),
        None,
    )
    if dims:
        size: tuple[float, float] | None = (float(dims.group(1)), float(dims.group(2)))
    else:
        size = named
    if size is None:
        if re.search(r"\b(?:page\s+size|resize\s+the\s+page|size\s+of\s+the\s+page)\b", low):
            return f.clarify(
                "What size should the page be? For example 1280 x 720 (16:9), 960 x 720 (4:3) "
                "or Letter."
            )
        return None
    if not all(100 <= side <= 20000 for side in size):
        # (Power BI's own limits: report_edits refuses anything outside them)
        return f.clarify(
            "A page can be between 100 and 20000 pixels on each side. What size should it be? "
            "For example 1280 x 720 (16:9), 960 x 720 (4:3) or Letter."
        )
    page = _page_or_ask(f)
    if isinstance(page, Clause):
        return page
    width, height = size
    if re.search(r"\bportrait\b", low) and width > height:
        width, height = height, width
    inside = [v for v in f.context.visuals_on(page.id) if not v.is_group]
    outside = [v for v in inside if v.x + v.width > width + 0.5 or v.y + v.height > height + 0.5]
    # set_page_size scales the visuals with the page unless told not to, so the parser sends the
    # operation alone (it works out every box itself, groups included); ``scale`` is only there
    # for a person who asked to keep the visuals where they are.
    page_op: Op = {"op": "set_page_size", "page": page.id, "width": width, "height": height}
    size_words = f"{plain_number(width)} x {plain_number(height)}"
    if _KEEP_VISUALS.search(low):
        if outside:
            return f.not_available(
                f"{len(outside)} of the {len(inside)} visuals on page '{page.name}' would fall "
                f"outside a {size_words} page, so they cannot stay where they are. Resize the "
                "page and let me scale the visuals, or make the page bigger."
            )
        page_op["scale"] = False
        return _page_result(
            f,
            [page_op],
            f"Resized page '{page.name}' to {size_words} and left its visuals where they are.",
            page,
        )
    if not outside:
        scaled = f" and scaled its {len(inside)} visuals with it" if inside else ""
        return _page_result(
            f, [page_op], f"Resized page '{page.name}' to {size_words}{scaled}.", page
        )
    return f.clarify(
        f"{len(outside)} of the {len(inside)} visuals on page '{page.name}' would fall outside a "
        f"{size_words} page. Scale everything to fit?",
        [
            Choice(
                label=f"Scale the visuals to fit ({len(inside)} visuals)",
                ops=(page_op,),
                summary=(
                    f"Resized page '{page.name}' to {size_words} and scaled its "
                    f"{len(inside)} visuals to fit."
                ),
            )
        ],
    )


# --- measures ------------------------------------------------------------------------------------

_TARGET_WORDS = re.compile(
    r"\b(?:target|threshold|goal|limit|cut[- ]?off|quota|benchmark|minimum|maximum)s?\b", re.I
)
_NUMERIC = re.compile(r"(?<![\w.\[])(\d[\d,]*(?:\.\d+)?)(?![\w.\]])")


def measure_hits(text: str, context: EditContext) -> list[MeasureEntry]:
    """Measures a request names, by whole name (longest first)."""
    words = tokens(text)
    found: list[tuple[int, MeasureEntry]] = []
    for measure in context.measures:
        wanted = tokens(measure.name)
        if not wanted:
            continue
        for i in range(len(words) - len(wanted) + 1):
            if words[i : i + len(wanted)] == wanted:
                found.append((len(wanted), measure))
                break
    found.sort(key=lambda item: -item[0])
    kept: list[MeasureEntry] = []
    for length, measure in found:
        if not any(set(tokens(measure.name)) < set(tokens(k.name)) for k in kept):
            kept.append(measure)
        del length
    return kept


def numeric_literals(expression: str) -> list[tuple[float, int, int]]:
    """(value, start, end) of each plain number in a DAX expression, outside brackets and quotes."""
    stripped = re.sub(r"'[^']*'|\[[^\]]*\]|\"[^\"]*\"", lambda m: " " * len(m.group(0)), expression)
    return [
        (float(m.group(1).replace(",", "")), m.start(1), m.end(1))
        for m in _NUMERIC.finditer(stripped)
    ]


def _replace_number(expression: str, old: float, new: float) -> str:
    out = expression
    for value, start, end in sorted(numeric_literals(expression), key=lambda t: -t[1]):
        if value == old:
            out = out[:start] + plain_number(new) + out[end:]
    return out


def _amounts(text: str) -> list[Amount]:
    return [a for a in find_amounts(text) if a.unit in {"", "k", "m", "b"}]


_BIGGEST = 1e15  # a number bigger than this is a typo, and DAX doubles lose digits past it
_UP_WORDS = re.compile(r"\b(?:raise|increase|bump|grow|up|higher|add)\b", re.I)
_DOWN_WORDS = re.compile(r"\b(?:lower|decrease|reduce|drop|cut|down|shrink|subtract)\b", re.I)


def _relative_change(text: str) -> tuple[int, Amount] | None:
    """(+1 or -1, the amount) of "raise the target by 10K" or "lower it by 5%"; None otherwise."""
    by = re.search(r"\bby\s+(?:about\s+|around\s+|roughly\s+)?", text, re.I)
    if by is None:
        return None
    amounts = [
        a
        for a in find_amounts(text[by.end() :])
        if a.unit in {"", "k", "m", "b", "%"} and a.start == 0
    ]
    if not amounts:
        return None
    before = text[: by.start()]
    down, up = _DOWN_WORDS.search(before), _UP_WORDS.search(before)
    if down is None and up is None:
        return None
    sign = -1 if down is not None and (up is None or down.start() > up.start()) else 1
    return sign, amounts[0]


def _bad_number(text: str) -> bool:
    """Scientific notation or a number too big to be a target: not something to write."""
    if re.search(r"\d[eE][+-]?\d", text):
        return True
    return any(abs(scaled(a)) > _BIGGEST for a in find_amounts(text) if a.unit != "%")


def _threshold_like(measure: MeasureEntry) -> bool:
    """A measure whose fixed number is a threshold or a target: a constant, or a number that
    a comparison uses. DIVIDE(3, 10) and 0 placeholders are not."""
    literals = [v for v, _, _ in numeric_literals(measure.expression) if v != 0]
    if not literals:
        return False
    body = measure.expression.strip()
    if re.fullmatch(r"[\d,.]+", body):
        return True
    return bool(re.search(r"[<>=]=?\s*[\d]|[\d]\s*[<>=]", body)) or bool(
        _TARGET_WORDS.search(measure.name)
    )


def _model_result(
    f: Frame, made: list[Op], summary: str, what_if: WhatIf | None = None, measure: str = ""
) -> Clause:
    target = Target((), None, f"[{measure}]" if measure else "the model", "measure")
    return Clause(
        f.text,
        ParseKind.MODEL_EDIT,
        ops=tuple(made),
        summary=summary,
        targets=(target,),
        what_if=what_if,
    )


def threshold_change(f: Frame, text: str, *, what_if_only: bool) -> Clause | None:
    """Read "change the 100K target to 120K": the measure holding 100000 gets 120000."""
    # The numbers as typed: a caption that says "100K" would otherwise be masked as the name of
    # a visual and the old number lost.
    source = text
    if _bad_number(source):
        return f.clarify(
            "I can only write a plain number, up to 15 digits, such as 120K or 120000. "
            "Which number should it be?"
        )
    relative = _relative_change(source)
    amounts = _amounts(source)
    if not amounts and relative is None:
        return None
    ctx = f.context
    old = new = None
    if relative is not None:
        amounts = []  # the amount is a change, not the new value
    elif len(amounts) >= 2:
        old, new = scaled(amounts[0]), scaled(amounts[1])
    else:
        new = scaled(amounts[0])
    if f.forced.old is not None:
        old = f.forced.old
    named = measure_hits(text, ctx)
    candidates = named
    entries = f.res.entries if f.res.ok and f.res.explicit or f.res.how == "selection" else []
    if old is not None and f.res.how in {"caption", "field"}:
        entries = []  # the old number finds its measure; "100K" is not the caption of a card
    if not candidates and entries:
        bound = {b[2].casefold() for e in entries for b in e.bindings if b[0] == "measure"}
        candidates = [m for m in ctx.measures if m.name.casefold() in bound]
    if f.forced.measure:
        candidates = [m for m in ctx.measures if m.name.casefold() == f.forced.measure.casefold()]
    if old is not None:
        holders = [
            m
            for m in (candidates or ctx.measures)
            if any(v == old for v, _, _ in numeric_literals(m.expression))
        ]
        if not candidates and len(holders) > 1:
            # "that card": a measure a card shows beats one no visual on the page uses
            named_by_text = f.res.how in {"caption", "field"}
            shown = [] if named_by_text else f.res.candidates or f.res.entries
            bound = {b[2].casefold() for e in shown for b in e.bindings if b[0] == "measure"}
            on_visuals = [m for m in holders if m.name.casefold() in bound]
            holders = on_visuals or holders
        if not candidates and len(holders) > 1 and _TARGET_WORDS.search(source):
            # "the target" is the measure that says so, not every measure that uses the number
            wording = [m for m in holders if _TARGET_WORDS.search(m.name)]
            holders = wording or holders
        candidates = holders
    else:
        with_number = [m for m in candidates if numeric_literals(m.expression)]
        if candidates and not with_number:
            # a measure named outright but with no fixed number: never pick another one
            names = ", ".join(f"[{m.name}]" for m in candidates[:3])
            return f.clarify(
                f"{names} has no fixed number to change. Say what it should calculate instead, "
                'for example "change the formula of Profit to SUM(FactSales[SalesAmount])".'
            )
        candidates = with_number
        if not candidates:
            wording = [
                m
                for m in ctx.measures
                if _TARGET_WORDS.search(m.name) and numeric_literals(m.expression)
            ]
            candidates = wording or [m for m in ctx.measures if _threshold_like(m)]
    if not candidates:
        holds = ", ".join(f"[{m.name}]" for m in ctx.measures if _threshold_like(m))[:300]
        lead = (
            f"No measure has {short_number(old)} in it. "
            if old is not None
            else "Which measure holds that number? "
        )
        return f.clarify(lead + (holds or "None of the measures has a fixed number."))
    if len(candidates) > 1:
        choices = [Choice(label=f"[{m.name}]", fill={"measure": m.name}) for m in candidates[:6]]
        return f.clarify(
            "More than one measure could hold that number: "
            + ", ".join(f"[{m.name}]" for m in candidates[:6])
            + ". Which one?",
            choices,
        )
    measure = candidates[0]
    literals = numeric_literals(measure.expression)
    if old is None:
        distinct = list(dict.fromkeys(v for v, _, _ in literals))
        if len(distinct) != 1:
            choices = [
                Choice(label=short_number(v), fill={"measure": measure.name, "old": v})
                for v in distinct[:6]
            ]
            listed = ", ".join(short_number(v) for v in distinct[:6])
            change = "the change applies to" if new is None else f"become {short_number(new)}"
            return f.clarify(
                f"[{measure.name}] has more than one number ({listed}). Which one should {change}?",
                choices,
            )
        old = distinct[0]
    if new is None and relative is not None:
        sign, delta = relative
        if delta.unit == "%":
            new = old * (1 + sign * delta.value / 100)
        else:
            new = old + sign * scaled(delta)
    if new is None or not abs(new) <= _BIGGEST:
        return f.clarify("That would give a number I cannot write. Give the new number itself.")
    new = round(new, 6)
    expression = _replace_number(measure.expression, old, new)
    question = f"What if [{measure.name}] used {short_number(new)} instead of {short_number(old)}?"
    what = WhatIf(question=question, measure=measure.name, new_expression=expression)
    if what_if_only:
        return _model_result(f, [], question, what, measure.name)
    edit: Op = {"op": "update_measure", "name": measure.name, "expression": expression}
    summary = (
        f"Change {short_number(old)} to {short_number(new)} in [{measure.name}] "
        "(needs Power BI, about 1 min; I show the what-if first)."
    )
    return _model_result(f, [edit], summary, what, measure.name)


_FORMAT_KINDS = (
    (re.compile(r"\bpercent(?:age)?s?\b|%"), "percent"),
    (re.compile(r"\bcurrenc(?:y|ies)\b|\bdollars?\b|\$"), "currency"),
    (re.compile(r"\beuros?\b|€"), "euro"),
    (re.compile(r"\bthousands?\b|\bin\s+k\b"), "thousands"),
    (re.compile(r"\bmillions?\b|\bin\s+m\b"), "millions"),
    (re.compile(r"\bwhole\s+numbers?\b|\bintegers?\b|\bno\s+decimals?\b"), "whole"),
    (re.compile(r"\bthousands?\s+separators?\b|\bcommas?\b"), "number"),
)


def measure_clause(f: Frame) -> Clause | None:
    low, m = f.low, f.masked
    ctx = f.context
    # create
    if re.search(
        r"\b(?:create|add|new|make|define|build)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:dax\s+)?measure\b",
        low,
    ):
        return _create_measure(f)
    # delete ("delete the measure Profit", and "delete the Profit measure", which was read as a
    # visual to delete and offered "All 6" visuals as the answer)
    if re.search(r"\b(?:delete|remove|drop)\s+(?:the\s+)?measure\b", low) or (
        re.search(r"\b(?:delete|remove|drop)\s+(?:the\s+)?(?P<name>.+?)\s+measure\s*[.!]?$", low)
        and measure_hits(f.text, ctx)
    ):
        hits = measure_hits(f.text, ctx)
        if len(hits) != 1:
            return f.clarify(
                "Which measure should be deleted? Name it, for example "
                '"delete the measure Profit Margin".'
            )
        return _model_result(
            f,
            [{"op": "delete_measure", "name": hits[0].name}],
            f"Delete measure [{hits[0].name}] (needs Power BI; it is refused while a visual "
            "uses it).",
            None,
            hits[0].name,
        )
    # rename
    if re.search(r"\brename\b", low):
        match = re.search(
            r"\brename\s+(?:the\s+)?(?:measure\s+)?(?P<old>.+?)\s+(?:measure\s+)?(?:to|as)\s+(?P<new>.+?)\s*[.!?]?$",
            f.text,
            re.I,
        )
        if match:
            hits = measure_hits(match.group("old"), ctx)
            named_measure = re.search(r"\bmeasure\b", low)
            captions = match_names(match.group("old"), [v for v in ctx.visuals if not v.hidden])
            if hits and (named_measure or not any(c.tier <= 2 for c in captions)):
                new = match.group("new").strip("\"' ")
                if ctx.measure(new):
                    return f.not_available(f"There is already a measure named '{new}'.")
                return _model_result(
                    f,
                    [{"op": "rename_measure", "name": hits[0].name, "new_name": new}],
                    f"Rename measure [{hits[0].name}] to [{new}] and update the visuals that use "
                    "it (needs Power BI).",
                    None,
                    hits[0].name,
                )
    # number format
    if re.search(r"\b(?:format|show|display|make|turn|change|set)\b", low):
        kind = next((name for pattern, name in _FORMAT_KINDS if pattern.search(m)), None)
        hits = measure_hits(f.text, ctx)
        wants_format = re.search(
            r"\b(?:format(?:ted)?|as\s+(?:a\s+)?(?:percent|currency|dollar|whole)|number\s+format)\b",
            low,
        )
        if kind and hits and wants_format and not find_types(m):
            digits = re.search(r"(\d+)\s+decimals?", low)
            decimals = (
                int(digits.group(1))
                if digits
                else (0 if re.search(r"\bno\s+decimals?|whole", low) else None)
            )
            code = (
                ops.format_string(
                    "euro" if kind == "euro" else kind, decimals, "€" if kind == "euro" else "$"
                )
                if kind != "euro"
                else "€#,0.00"
            )
            return _model_result(
                f,
                [{"op": "update_measure", "name": hits[0].name, "format_string": code}],
                f"Format [{hits[0].name}] as {kind} ({code}); needs Power BI, about 1 min.",
                None,
                hits[0].name,
            )
    # a named measure gets a new formula, or a new number
    redefined = _redefine_measure(f)
    if redefined is not None:
        return redefined
    # thresholds
    verb = re.search(
        r"\b(?:change|set|update|edit|raise|lower|increase|decrease|reduce|bump|adjust|make|move|"
        r"put)\b",
        low,
    )
    if (
        verb
        and (_TARGET_WORDS.search(m) or re.search(r"\bmeasure\b|\bformula\b|\bexpression\b", m))
        and (_amounts(m) or _relative_change(m))
    ):
        return threshold_change(f, f.text, what_if_only=False)
    pair = _old_to_new(m) if verb else None
    if pair is not None and _has_number_holder(ctx, pair):
        # "change the 100K to 120K": the old number finds the measure that holds it
        return threshold_change(f, f.text, what_if_only=False)
    if (
        verb
        and f.res.how == "selection"
        and f.res.pronoun
        and re.search(r"\bto\s+\d", m)
        and _selection_measures(f)
    ):
        return threshold_change(f, f.text, what_if_only=False)  # "change it to 120K" on a card
    if verb and re.search(r"\b(?:it|this|that)\s+to\s+\d[\d,.]*\s*[kmb]\b|\bto\s+\d{4,}\b", m):
        return f.clarify(
            "Which number should change? Select the visual that shows it, or name the measure, "
            'for example "change the 100K target to 120K".'
        )
    return None


def _old_to_new(m: str) -> tuple[float, float] | None:
    """(old, new) of "the 100K to 120K" or "from 100K to 120K"; None otherwise."""
    found = re.search(
        r"\b(?:from\s+|the\s+)?(?P<a>\d[\d,.]*\s*[kmb]?)\s+(?:to|into|with)\s+(?P<b>\d[\d,.]*\s*[kmb]?)\b",
        m,
        re.I,
    )
    if found is None:
        return None
    first, second = find_amounts(found.group("a")), find_amounts(found.group("b"))
    if not first or not second:
        return None
    return scaled(first[0]), scaled(second[0])


def _has_number_holder(context: EditContext, pair: tuple[float, float]) -> bool:
    return any(v == pair[0] for m in context.measures for v, _, _ in numeric_literals(m.expression))


def _selection_measures(f: Frame) -> bool:
    bound = {b[2].casefold() for e in f.res.entries for b in e.bindings if b[0] == "measure"}
    return any(
        m.name.casefold() in bound and numeric_literals(m.expression) for m in f.context.measures
    )


_REDEFINE_VERB = re.compile(
    r"\b(?:update|change|set|edit|modify|redefine|rewrite|replace|redo|define|make)\b", re.I
)
_MEASURE_WORD = re.compile(r"\b(?:measure|formula|expression|definition|calculation)\b", re.I)
_TAKES_VALUE = re.compile(
    r"^\s*(?:measure\s+)?(?:(?:to|as|=|:|equal\s+to|equals?)\s*)(?P<value>.+?)\s*$", re.I | re.S
)


def _looks_like_dax(text: str) -> bool:
    return bool(re.search(r"[\[\(]", text)) or bool(re.search(r"\w\s*[-+*/]\s*\w", text))


def _balanced(text: str) -> bool:
    depth = {"(": 0, "[": 0}
    closer = {")": "(", "]": "["}
    for char in re.sub(r"\"[^\"]*\"", "", text):
        if char in depth:
            depth[char] += 1
        elif char in closer:
            depth[closer[char]] -= 1
            if depth[closer[char]] < 0:
                return False
    return not any(depth.values())


def _redefine_measure(f: Frame) -> Clause | None:
    """ "update the measure Profit to [Total Sales] * 2": the measure named first is the one
    that changes, never another one that happens to hold a number."""
    if not _REDEFINE_VERB.search(f.low):
        return None
    for measure in sorted(f.context.measures, key=lambda m: -len(m.name)):
        words = re.escape(measure.name).replace(r"\ ", r"\s+")
        found = re.search(rf"(?<![\w\[-]){words}(?![\w-])", f.text, re.I)
        if found is None:
            continue
        taken = _TAKES_VALUE.match(f.text[found.end() :])
        if taken is None:
            continue
        head = f.text[: found.start()]
        after = f.text[found.end() :][:12]
        names_measure = _MEASURE_WORD.search(head) or _MEASURE_WORD.search(after)
        if not names_measure and not _looks_like_dax(taken.group("value")):
            continue
        value = taken.group("value").strip().rstrip(".")
        if _amounts(value) and not _looks_like_dax(value) and len(find_amounts(value)) == 1:
            forced = replace(f.forced, measure=measure.name)
            return threshold_change(replace(f, forced=forced), f.text, what_if_only=False)
        if not _looks_like_dax(value) or len(value) > 4000 or not _balanced(value):
            return f.clarify(
                f"What should [{measure.name}] calculate? Give the formula, for example "
                '"= SUM(FactSales[SalesAmount])", with matching brackets.'
            )
        return _model_result(
            f,
            [{"op": "update_measure", "name": measure.name, "expression": value}],
            f"Change the formula of [{measure.name}] to {value} (needs Power BI, about 1 min).",
            None,
            measure.name,
        )
    return None


# A request that makes or changes a security role ("create a role Store Team that ...",
# "add an RLS role ...", "make the Online Team role see only ...").
_ROLE_REQUEST = re.compile(
    r"\b(?:create|add|make|define|set\s+up|new|build|change|update|edit)\s+(?:a\s+|an\s+|the\s+)?"
    r"(?:new\s+)?(?:(?:rls|security|row[\s-]level(?:\s+security)?)\s+)?roles?\b(?!\s+of\b)"
    r"|\b(?:rls|security|row[\s-]level(?:\s+security)?)\s+roles?\b",
    re.I,
)


def _create_measure(f: Frame) -> Clause | None:
    ctx = f.context
    name = _value_after(f.text, "called|named|titled") or None
    expression = None
    eq = re.search(
        r"=\s*(?P<eq>\S.*)$|"
        r"(?:\bas\b|\busing\b|\bwith\s+(?:the\s+)?(?:formula|expression|dax)\b|:)\s*(?P<dax>.*[\(\[].*)$",
        f.text,
        re.I,
    )
    if eq:
        expression = (eq.group("eq") or eq.group("dax")).strip().rstrip(".")
        if name is None:
            before = f.text[: eq.start()]
            called = re.search(
                r"measure\s+(?:called\s+|named\s+)?[\"']?([\w %#/().-]+?)[\"']?\s*$", before, re.I
            )
            name = called.group(1).strip() if called else None
    if name:
        # the name ends where the definition starts: "Foo = SUM(...)", "Foo as SUM(...)"
        name = re.split(
            r"\s*(?:=|:)\s*|\s+(?:that|which|as|to|using|for|with|=|:)\s+", name, maxsplit=1
        )[0].strip("\"' ")
    if not name:
        return f.clarify("What should the new measure be called, and what should it calculate?")
    if len(name) > 100 or not re.fullmatch(r"[\w %#/&.'-]+", name):
        return f.clarify(
            "A measure name can have letters, digits and spaces, without brackets or an equals "
            'sign. What should it be called? For example "Profit Margin".'
        )
    if ctx.measure(name):
        return f.not_available(f"There is already a measure named '{name}'.")
    if expression is None and new_measure_calculation(f.text) is not None:
        # A stated business calculation goes to the bounded local drafting tier. A name
        # alone still asks below; a typed definition stays in the deterministic path.
        return None
    if expression is None:
        expression = _derive_expression(f, name)
    if expression is None:
        return f.clarify(
            f"What should [{name}] calculate? Give the formula, for example "
            '"= SUM(FactSales[SalesAmount])", or say "the sum of SalesAmount".'
        )
    table = _home_table(ctx)
    if table is None:
        return f.not_available("The model has no table to hold a new measure.")
    return _model_result(
        f,
        [{"op": "create_measure", "table": table, "name": name, "expression": expression}],
        f"Create measure [{name}] in {table}: {expression} (needs Power BI, about 1 min).",
        None,
        name,
    )


def _home_table(context: EditContext) -> str | None:
    if context.measures:
        counts: dict[str, int] = {}
        for m in context.measures:
            counts[m.table] = counts.get(m.table, 0) + 1
        return max(counts, key=lambda t: counts[t])
    return next(iter(context.tables), None)


_AGGREGATES = (
    (r"\b(?:sum|total)\s+(?:of\s+)?", "SUM"),
    (r"\b(?:average|mean|avg)\s+(?:of\s+)?", "AVERAGE"),
    (r"\bdistinct\s+count\s+(?:of\s+)?|\bunique\s+(?:count\s+of\s+)?", "DISTINCTCOUNT"),
    (r"\bcount\s+(?:of\s+)?", "COUNT"),
    (r"\b(?:max(?:imum)?|highest|largest)\s+(?:of\s+)?", "MAX"),
    (r"\b(?:min(?:imum)?|lowest|smallest)\s+(?:of\s+)?", "MIN"),
)


def _ref(hit: ops.FieldHit) -> str:
    table = f"'{hit.table}'" if re.search(r"[^\w]", hit.table) else hit.table
    return f"[{hit.name}]" if hit.kind == "measure" else f"{table}[{hit.name}]"


def _derive_expression(f: Frame, name: str) -> str | None:
    """A DAX expression from "the sum of SalesAmount" or "A divided by B"; None when unclear."""
    tail = f.text
    hits = [h for h in ops.match_fields(tail, f.context) if h.name.casefold() != name.casefold()]
    if not hits:
        return None
    ratio = re.search(
        r"\b(?:divided\s+by|over|per|/|as\s+a\s+(?:percent(?:age)?|ratio|share)\s+of)\b", tail, re.I
    )
    if ratio and len(hits) >= 2:
        first = next(h for h in hits if h.start < ratio.start())
        second = next(h for h in hits if h.start > ratio.end() - 1)
        return f"DIVIDE({_wrap(first)}, {_wrap(second)})"
    minus = re.search(r"\b(?:minus|less|-)\b", tail, re.I)
    if minus and len(hits) >= 2:
        first = next(h for h in hits if h.start < minus.start())
        second = next(h for h in hits if h.start > minus.end() - 1)
        return f"{_wrap(first)} - {_wrap(second)}"
    hit = hits[0]
    if hit.kind == "measure":
        return _ref(hit)
    for pattern, function in _AGGREGATES:
        if re.search(pattern, tail[: hit.start], re.I):
            return f"{function}({_ref(hit)})"
    return None


def _wrap(hit: ops.FieldHit) -> str:
    if hit.kind == "measure":
        return _ref(hit)
    return f"SUM({_ref(hit)})"


def what_if_clause(frame: Frame) -> Clause | None:
    """A "what if the target were 120K" is a preview request; nothing is edited or applied."""
    if not _amounts(frame.masked):
        return None
    if not (
        _TARGET_WORDS.search(frame.masked)
        or measure_hits(frame.text, frame.context)
        or re.search(r"\b\d+\s*[km]\b", frame.masked, re.I)
    ):
        return None
    return threshold_change(frame, frame.text, what_if_only=True)


# --- adding visuals ------------------------------------------------------------------------------

_ADD_VERB = (
    r"(?:add|insert|create|build|make|put|place|draw|include|show|give|plot|want|need|see|display)"
)
_ARTICLE = r"(?:(?:a|an|another|one|one\s+more|some|new|the\s+following)\s+)"
_ADD_MODIFIERS = (
    r"(?:(?:new|clustered|stacked|100%\s+stacked|simple|basic|nice|small|big|large|"
    r"\d{2,5}(?:\.\d+)?\s*(?:px)?\s*(?:x|×|by)\s*\d{2,5}(?:\.\d+)?\s*(?:px)?)\s+)*"
)


def _type_alternatives() -> str:
    return "|".join(
        re.escape(t).replace(r"\ ", r"[\s-]+") for t in sorted(ops.ADD_TYPES, key=len, reverse=True)
    )


_ADD_RE = re.compile(
    rf"\b{_ADD_VERB}\s+(?:me\s+)?{_ARTICLE}+{_ADD_MODIFIERS}(?P<type>{_type_alternatives()}|graph|graphs)"
    r"(?:e?s)?\b",
    re.I,
)
_WISH_RE = re.compile(
    rf"^\s*{_ARTICLE}+{_ADD_MODIFIERS}(?P<type>{_type_alternatives()}|graph|graphs)(?:e?s)?\b", re.I
)


MAX_NEW_VISUALS = 12  # visuals one request may add: each is placed around the ones before it


def add_visual_clause(f: Frame) -> Clause | None:
    match = _ADD_RE.search(f.text) or _WISH_RE.search(f.text)
    if not match:
        return None
    if re.search(
        r"\b(?:add|insert|create)\s+(?:a\s+|an\s+)?(?:new\s+)?(?:page|tab|measure|column)\b", f.low
    ):
        return None
    words = re.sub(r"\bgraphs?\b", "chart", match.group("type"), flags=re.I)
    key = normalise_type(words)
    stacked = re.search(r"\bstacked\b", match.group(0), re.I) and key in {
        "column chart",
        "bar chart",
    }
    hundred = "100%" in match.group(0)
    if key is None:
        return None
    if stacked:
        key = "stacked " + key
    if hundred:
        key = "100% stacked " + key.replace("stacked ", "")
    visual_type = ops.ADD_TYPES.get(key, "")
    if not visual_type:
        return None
    ctx = f.context
    page = f.res.page
    if page is None:
        # "add a card to page 2": for a new visual the destination is the page that is meant
        by_name, _ = find_page(f.text, ctx)
        page = by_name or _active_page(ctx)
    fresh = re.search(
        r"\b(?:to|on|onto|in|into)\s+(?:a\s+|an\s+|the\s+)?(?:new|blank|separate|another|fresh)\s+"
        r"(?:page|tab)\b",
        f.low,
    )
    new_page: Op | None = None
    if fresh:
        # "to a new page": the page is added first, in the same revision, and named by its name
        reference = page or (ctx.pages[-1] if ctx.pages else None)
        name = _unique_page_name(ctx, f"Page {len(ctx.pages) + 1}")
        new_page = {"op": "add_page", "name": name}
        if reference is not None:
            new_page["width"], new_page["height"] = reference.width, reference.height
            new_page["after"] = reference.id
        page = PageEntry(
            id=name,
            name=name,
            index=len(ctx.pages),
            width=reference.width if reference else 1280.0,
            height=reference.height if reference else 720.0,
        )
    if page is None:
        choices = [
            Choice(label=p.name, fill={"append": f"on page {p.index + 1}"}) for p in ctx.pages[:8]
        ]
        return f.clarify("On which page should it go?", choices)
    exclude: list[tuple[int, int]] = [match.span("type")]
    exclude += find_page(f.text, ctx)[1]
    named = re.search(r"\b(?:called|named|titled)\s+[\"']?([^\"',]+)[\"']?", f.text, re.I)
    if named:
        exclude.append(named.span())
    hits = ops.match_fields(f.text, ctx, exclude, partial=True)
    if not hits:
        return _no_fields(f, key)
    title = named.group(1).strip() if named else None
    built = _build_add(f, key, visual_type, hits, page, title)
    if new_page is not None and built.kind is ParseKind.APPLY:
        built = replace(
            built,
            ops=(new_page, *built.ops),
            summary=built.summary.replace(f"to page '{page.name}'", f"to a new page '{page.name}'"),
        )
    return built


def _no_fields(f: Frame, key: str) -> Clause:
    ctx = f.context
    words = [
        w
        for w in re.findall(r"[A-Za-z][\w%]+", f.text)
        if w.casefold()
        not in {
            "add",
            "a",
            "an",
            "the",
            "of",
            "by",
            "for",
            "with",
            "create",
            "chart",
            "table",
            "card",
        }
    ]
    pool = [m.name for m in ctx.measures] + [c for cols in ctx.tables.values() for c in cols]
    close: list[str] = []
    for word in words:
        close += difflib.get_close_matches(word, pool, n=2, cutoff=0.6)
    close = list(dict.fromkeys(close))[:4]
    choices = [Choice(label=name, fill={"append": f"of {name}"}) for name in close]
    if choices:
        return f.clarify(
            f"Which fields should the {key} show? Closest names in the model: "
            + ", ".join(close)
            + ".",
            choices,
        )
    measures = [m.name for m in ctx.measures[:3]]
    example = f' For example: "add a {key} of {measures[0]}".' if measures else ""
    return f.clarify(
        f"Which fields should the {key} show? I could not find a measure or column named in "
        f"your message.{example}"
    )


def _role_after(text: str, hit: ops.FieldHit) -> str | None:
    tail = text[hit.end : hit.end + 40]
    found = re.match(r"\s*(?:on|in|as|to|for)\s+(?:the\s+)?(rows?|columns?|values?)\b", tail, re.I)
    return found.group(1).casefold().rstrip("s") if found else None


def _build_add(
    f: Frame,
    key: str,
    visual_type: str,
    hits: list[ops.FieldHit],
    page: PageEntry,
    title: str | None,
) -> Clause:
    ctx = f.context
    measures = [h for h in hits if h.kind == "measure"]
    columns = [h for h in hits if h.kind == "column"]

    def numeric(h: ops.FieldHit) -> bool:
        kind = ctx.column_types.get(f"{h.table}.{h.name}", "").casefold()
        return any(t in kind for t in ("int", "float", "decimal", "number"))

    before_by = re.search(
        r"\b(?:by|per|across|for\s+each|over|split\s+by|broken\s+down\s+by)\b", f.low
    )
    if not measures and before_by:
        numerics = [h for h in columns if numeric(h) and h.start < before_by.start()]
        if numerics:
            measures = numerics
            columns = [h for h in columns if h not in numerics]
    specs: list[list[ops.FieldHit]] = []
    if visual_type in {"card", "kpi", "gauge"}:
        if not measures:
            return f.clarify(
                f"Which measure should the {key} show?",
                [Choice(label=m.name, fill={"append": f"of {m.name}"}) for m in ctx.measures[:5]],
            )
        specs = [[m] for m in measures]
    elif visual_type == "multiRowCard":
        specs = [measures + columns]
    elif visual_type == "slicer":
        if not columns:
            return f.clarify("Which field should the slicer use?")
        specs = [[c] for c in columns]
    elif visual_type in {"tableEx"}:
        specs = [hits]
    elif visual_type == "pivotTable":
        rows = [h for h in columns if _role_after(f.text, h) in {"row", None}]
        cols = [h for h in columns if _role_after(f.text, h) in {"column"}]
        rows = [h for h in rows if h not in cols]
        if len(rows) > 1 and not cols and not any(_role_after(f.text, h) for h in columns):
            cols = rows[1:2]
            rows = rows[:1]
        specs = [[*rows, *cols, *measures]]
    else:
        if not measures:
            return f.clarify(
                f"Which measure should the {key} show?",
                [Choice(label=m.name, fill={"append": f"of {m.name}"}) for m in ctx.measures[:5]],
            )
        if not columns and visual_type not in {"gauge", "kpi"}:
            specs = [measures]
        else:
            category = columns
            if visual_type in {"lineChart", "areaChart", "stackedAreaChart"}:
                dates = [c for c in columns if ops.is_date_column(ctx, c.table, c.name)]
                category = dates or columns
            specs = [[*measures, *category[:1]]]
    if len(specs) > MAX_NEW_VISUALS:
        return f.clarify(
            f"That would add {len(specs)} visuals at once. Add up to {MAX_NEW_VISUALS} at a time, "
            "so each one can be placed."
        )
    made: list[Op] = []
    described: list[str] = []
    # A new visual goes where the operation finds free space (it sees the visuals added before
    # it in the same batch); a place and a size are only sent when the request gives numbers.
    box = explicit_box(f.text)
    for spec in specs:
        refs: list[dict[str, Any]] = []
        for h in spec:
            ref: dict[str, Any] = h.ref()
            if h.kind == "column" and h in measures:
                ref["aggregation"] = "Sum"
            refs.append(ref)
        op: Op = {
            "op": "add_visual",
            "page": page.id,
            "visual_type": visual_type,
            "fields": refs,
            **box,
        }
        chart_like = visual_type not in {
            "card",
            "multiRowCard",
            "tableEx",
            "pivotTable",
            "slicer",
            "kpi",
            "gauge",
        }
        if title:
            op["title"] = title
        elif chart_like:
            op["title"] = ops.chart_title(
                [h for h in spec if h.kind == "measure" or h in measures],
                [h for h in spec if h.kind == "column" and h not in measures],
            )
        made.append(op)
        if chart_like:
            described.append(
                ops.chart_title(
                    [h for h in spec if h.kind == "measure" or h in measures],
                    [h for h in spec if h.kind == "column" and h not in measures],
                )
            )
        else:
            described.append(", ".join(h.name for h in spec))
    label = "a" if key[0] not in "aeiou" else "an"
    # a place said in words ("in the top right", "below the table") is not something the operation
    # can honor yet; the receipt says so instead of leaving the person to find out
    spot = SPOT_NOT_HONORED if "x" not in box and PLACE_WORDS.search(f.text) else ""
    summary = f"Added {label} {key} of {'; '.join(described)} to page '{page.name}'{spot}."
    if len(specs) > 1:
        summary = f"Added {len(specs)} {key}s ({'; '.join(described)}) to page '{page.name}'{spot}."
    target = Target((), page.id, f"page '{page.name}'", "page")
    return Clause(f.text, ParseKind.APPLY, ops=tuple(made), summary=summary, targets=(target,))


def _without_values(f: Frame) -> str:
    """The clause with the words that name a target or give a new name or text blanked out, for
    finding what it asks for: "rename the Details page to Filters" does not ask for a filter."""
    # From the text as typed: the resolver's mask also blanks a page called Forecast in "add a
    # forecast line"; names that read like a feature are blanked by Parser.strip_names instead.
    chars = list(f.low)
    spans = [(start, end) for start, end, _ in quoted(f.text)]
    text_span = explicit_text_span(f)
    if text_span:
        spans.append(text_span)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


# --- the parser ----------------------------------------------------------------------------------


class Parser:
    def __init__(self, context: EditContext) -> None:
        self.context = context
        self._names: list[str] | None = None
        self._risky_names: list[str] | None = None

    def names(self) -> list[str]:
        """Every name a clause may start with that is not a verb (fields, captions, pages)."""
        if self._names is None:
            ctx = self.context
            names = [m.name for m in ctx.measures] + [
                c for cols in ctx.tables.values() for c in cols
            ]
            names += [v.caption for v in ctx.visuals if v.caption] + [p.name for p in ctx.pages]
            names += [f for v in ctx.visuals for f in v.fields] + list(ctx.tables)
            self._names = [n for n in dict.fromkeys(names) if len(n) > 2]
        return self._names

    def strip_names(self, text: str) -> str:
        """The text with names that read like a feature request blanked: a page called Forecast
        is not a request for a forecast line, and "the Forecast page" is not one either."""
        if self._risky_names is None:
            names = set(self.names())
            # (a name with a color or a style word in it, "Product Color" or "Blue Team", is not
            # a styling request either)
            self._risky_names = sorted(
                (
                    n.strip()
                    for n in names
                    if len(n.strip()) > 2 and (detect_capability(n) or reads_like_styling(n))
                ),
                key=len,
                reverse=True,
            )
        for name in self._risky_names:
            text = re.sub(
                r"(?<![\w-])" + re.escape(name).replace(r"\ ", r"\s+") + r"(?![\w-])"
                r"(?!\s+(?:lines?|bands?))",
                " ",
                text,
                flags=re.I,
            )
        return text

    def has_object(self, text: str) -> bool:
        from .edit_text import REPORT_NOUNS

        if REPORT_NOUNS.search(text):
            return True
        ctx = self.context
        pair = _old_to_new(text)
        return bool(
            match_names(text, [v for v in ctx.visuals if not v.is_group])
            or ops.match_fields(text, ctx)
            or measure_hits(text, ctx)
            or find_page(text, ctx)[0]
            or find_colors(text)
            or looks_like_filter(text)
            or (pair is not None and _has_number_holder(ctx, pair))
        )

    def frame(
        self,
        text: str,
        carry: Sequence[VisualEntry],
        forced: Forced | None,
    ) -> Frame:
        forced = forced or Forced()
        if forced.append:
            text = f"{text} {forced.append}"
        ctx = self.context
        low = text.casefold()
        blank = Frame(ctx, text, low, low, Resolution(), forced, list(carry))
        exclude = []
        span = explicit_text_span(blank)
        if span:
            exclude.append(span)
        res = resolve_visuals(text, ctx, carry=list(carry), exclude=exclude, forced=forced.visuals)
        return Frame(ctx, text, low, res.masked.casefold(), res, forced, list(carry))

    def styling(self, text: str) -> Clause | None:
        """The styling refusal when the words ask to change how something looks (a color, a
        font, a title, an axis's range...), else None. For words no recognizer took as an edit
        ("end the y axis at 50000", "draw a frame around the slicer"): the chat refuses them
        instead of answering them as a question or handing them to the model."""
        f = self.frame(text, (), None)
        words = self.strip_names(_without_values(f))
        if is_styling(words):
            return f.unsupported(capability("styling"))
        if styling_gives_way(words):
            return None  # a measure, a name, a filter, a text box, a number format
        claimed = detect_capability(words)
        if claimed is not None and claimed.status == "supported":
            return None  # a feature a tool family provides ("set the alt text to Green bars")
        if styling_signal(f, parts_alone=True):
            return f.unsupported(capability("styling"))
        return None

    def clause(
        self,
        text: str,
        carry: Sequence[VisualEntry] = (),
        forced: Forced | None = None,
    ) -> Clause | None:
        """The clause read as an edit, a question to ask or a refusal; None when unreadable."""
        f = self.frame(text, carry, forced)
        capability = detect_capability(self.strip_names(_without_values(f)))
        if capability is not None and capability.status != "supported":
            return f.unsupported(capability)
        if capability is not None:
            # A feature a tool family added after this parser was written ("add a data bar"):
            # the recognizers below would read it as something that only looks similar (an
            # average line as a new line chart), so it is left to the tool cards and the planner.
            return None
        if new_measure_calculation(f.text) is not None:
            # This is a measure request even when its natural description is left unread.
            # Later visual recognizers must not turn its "called" name into a chart title.
            return _create_measure(f)
        if _ROLE_REQUEST.search(f.text):
            # "add a role Online Team that can only see the Online channel" makes a security
            # role: what its members see is its row filter, never a filter on this page
            return None
        # Text boxes and filters come before the page family: "give the page a title" and "only
        # show 2019 on this page" have the word page in them but are not about a page.
        for family in (
            add_object_clause,
            filter_clause,
            page_clause,
            measure_clause,
            add_visual_clause,
        ):
            found = family(f)
            if found is not None:
                return found
        found = visual_clause(f)
        if found is not None:
            return found
        found = vague_clause(f)
        if found is not None:
            return found
        if re.search(r"\b(?:rename|retitle)\b", f.low):
            return f.clarify(
                "Rename what, and to what? For example: rename the 'Country' slicer to 'Region', "
                "or rename page 1 to Summary."
            )
        return None


__all__ = [
    "CAPABILITY_MATRIX",
    "Parser",
    "find_new_type",
    "meta_clause",
    "measure_hits",
    "numeric_literals",
    "type_name",
    "what_if_clause",
]
