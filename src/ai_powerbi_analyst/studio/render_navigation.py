"""What makes an imported report navigable in Studio's view mode: bookmarks, button and
navigator actions, drillthrough and tooltip pages, visual interactions, synced slicers and
the phone layout.

Only reading happens here. ``live_report`` applies a bookmark or drill values to a page
render; the canvas follows actions (go to a page, apply a bookmark) itself. Shapes that no
Desktop-authored file in hand shows are listed in ``UNVERIFIED_SHAPES``.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import pbir_expr
from .report_import import ImportedPage, ImportedVisual
from .report_props import properties

Json = dict[str, Any]

BOOKMARKS = "Report/definition/bookmarks/"
_BOOKMARK_PART = re.compile(re.escape(BOOKMARKS) + r"([^/]{1,200})\.bookmark\.json")
_MAX_PART_BYTES = 2_000_000
_MAX_BOOKMARKS = 500

UNVERIFIED_SHAPES: dict[str, str] = {
    "bookmark": (
        "explorationState.sections.<page>.visualContainers.<visual>.singleVisual."
        "{display: {mode: 'hidden'}, objects.merge.general[].properties.filter (plain "
        "objects.general read too)}; options {targetVisualNames, applyOnlyToTargetVisuals, "
        "suppressData, suppressDisplay, suppressActiveSection}; a whole-page bookmark shows the "
        "visuals and clears the slicers it does not list (the navigation family read this in "
        "Desktop's serializer; the multipage fixture's bookmarks are hand-built)"
    ),
    "bookmark.visualContainerGroups": "a tree {<group>: {isHidden, children: {...}}}",
    "bookmark.filters.byExpr": (
        "Report, page and visual filter states as filterConfig-like lists keyed by filter name"
    ),
    "bookmark.filters.byName/byType": (
        "byName {<filter name>: state} for a filter hidden or locked in view mode (a list is "
        "read too), byType [state] for include/exclude filters, as the navigation family "
        "writes them from Desktop's capture; a state without a name is matched by its "
        "expression"
    ),
    "pageNavigator.pages.showPage": "Per-page entries under a selector {id: <page name>}",
    "bookmarkNavigator.bookmarks.bookmarkGroup": "The group's name or display name",
    "mobile.json": "visualContainerMobileState: position only (the fixture's is hand-built)",
    "visualTooltip": "{show, type: 'Canvas' (Desktop's code) or 'ReportPage', section: <page>}",
    "visualLink": "{show, type: Back|Bookmark|PageNavigation|Drillthrough|WebUrl|QnA, ...}",
    "pageBinding.acceptsFilterContext": "'None' when 'Keep all filters' is off",
    "pageBinding.referenceScope": "'CrossReport' for a drillthrough other reports may target",
    "pageBinding.parameters[].boundFilter": (
        "The name of the page filter that holds the drilled field's saved value; a drill "
        "value replaces it"
    ),
    "syncGroup": (
        "{groupName, fieldChanges, filterChanges}: slicers of one group share their "
        "selection across pages when filterChanges is on (fieldChanges: a field change "
        "applies to all)"
    ),
    "visualInteractions": (
        "[{source, target, type}] with type NoFilter | DataFilter | Filter | HighlightFilter "
        "| Default; another type is reported 'unsupported'"
    ),
    "settings.defaultFilterActionIsDataFilter": (
        "report.json settings flag: cross-filter instead of highlight by default"
    ),
    "bookmark display modes": (
        "singleVisual.display.mode other than hidden: spotlight, maximize/focus (focus mode), "
        "elevation (layer order); not drawn, the bookmark notes name them"
    ),
    "bookmark objects.merge": (
        "singleVisual.objects.merge.general[].properties.filter holds a slicer's saved "
        "selection (objects.general in older files)"
    ),
    "bookmark orderBy/activeProjections": (
        "orderBy [{Direction 1 ascending | 2 descending, Expression}] and activeProjections "
        "{role: [fields]}: noted only when they differ from the visual's sortDefinition and "
        "its projections' active flags"
    ),
}

# Visual types that cross-highlight when another visual is clicked (their capabilities say
# supportsHighlight). Every other target is cross-filtered.
HIGHLIGHT_TYPES = frozenset(
    {
        "barChart",
        "clusteredBarChart",
        "clusteredColumnChart",
        "columnChart",
        "stackedBarChart",
        "stackedColumnChart",
        "pieChart",
        "donutChart",
        "hundredPercentStackedBarChart",
        "hundredPercentStackedColumnChart",
    }
)
# Desktop's visualInteractions types, and what Studio does for each.
INTERACTION_TYPES = {
    "NoFilter": "none",
    "DataFilter": "filter",
    "Filter": "filter",
    "HighlightFilter": "highlight",
}


def _read_json(z: zipfile.ZipFile, part: str) -> Json | None:
    try:
        info = z.getinfo(part)
        if info.file_size > _MAX_PART_BYTES:
            return None
        found = json.loads(z.read(info).decode("utf-8-sig"))
    except (KeyError, ValueError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    return found if isinstance(found, dict) else None


# --- bookmarks ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Bookmark:
    name: str
    display_name: str
    group: tuple[str, str] | None
    body: Json

    @property
    def options(self) -> Json:
        found = self.body.get("options")
        return found if isinstance(found, dict) else {}

    @property
    def page(self) -> str | None:
        found = _exploration(self.body).get("activeSection")
        return found if isinstance(found, str) else None

    @property
    def captures(self) -> Json:
        """What applying the bookmark changes: filters and slicers, what is shown, the page."""
        options = self.options
        return {
            "data": options.get("suppressData") is not True,
            "display": options.get("suppressDisplay") is not True,
            "page": options.get("suppressActiveSection") is not True,
        }

    @property
    def targets(self) -> list[str] | None:
        """The visuals a "selected visuals" bookmark applies to; None for the whole page."""
        options = self.options
        names = options.get("targetVisualNames")
        if options.get("applyOnlyToTargetVisuals") is True and isinstance(names, list):
            return [n for n in names if isinstance(n, str)]
        return None

    def section(self, page_id: str) -> Json:
        sections = _exploration(self.body).get("sections")
        found = sections.get(page_id) if isinstance(sections, dict) else None
        return found if isinstance(found, dict) else {}

    def containers(self, page_id: str) -> dict[str, Json]:
        found = self.section(page_id).get("visualContainers")
        if not isinstance(found, dict):
            return {}
        targets = self.targets
        return {
            k: v
            for k, v in found.items()
            if isinstance(v, dict) and (targets is None or k in targets)
        }


def _exploration(body: Json) -> Json:
    found = body.get("explorationState")
    return found if isinstance(found, dict) else {}


def read_bookmarks(z: zipfile.ZipFile) -> list[Bookmark]:
    """The report's bookmarks in the order of the Bookmarks pane (groups flattened)."""
    parts = {}
    for name in z.namelist():
        match = _BOOKMARK_PART.fullmatch(name)
        if match:
            parts[match.group(1)] = name
        if len(parts) > _MAX_BOOKMARKS:
            break
    meta = _read_json(z, BOOKMARKS + "bookmarks.json") or {}
    order: list[tuple[str, tuple[str, str] | None]] = []
    items = meta.get("items")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        children = item.get("children")
        if isinstance(children, list):
            pair = (str(item["name"]), str(item.get("displayName") or item["name"]))
            order.extend((c, pair) for c in children if isinstance(c, str))
        else:
            order.append((item["name"], None))
    listed = {name for name, _ in order}
    order.extend((name, None) for name in sorted(parts) if name not in listed)
    found: list[Bookmark] = []
    for name, group in order:
        part = parts.get(name)
        body = _read_json(z, part) if part else None
        if body is None:
            continue
        display = body.get("displayName")
        found.append(Bookmark(name, display if isinstance(display, str) else name, group, body))
    return found


def find_bookmark(bookmarks: Sequence[Bookmark], name: str) -> Bookmark:
    """A bookmark by its name or, failing that, its display name (any case)."""
    for bookmark in bookmarks:
        if bookmark.name == name:
            return bookmark
    wanted = name.casefold()
    matches = [b for b in bookmarks if b.display_name.casefold() == wanted]
    if len(matches) != 1:
        raise ValueError(f"This report has no bookmark named {name[:80]!r}")
    return matches[0]


def _hidden_of(container: Json) -> bool | None:
    single = container.get("singleVisual")
    display = single.get("display") if isinstance(single, dict) else None
    if isinstance(display, dict) and isinstance(display.get("mode"), str):
        return str(display["mode"]).casefold() == "hidden"
    return None


def _group_states(groups: Any, found: dict[str, bool], depth: int = 0) -> None:
    """``visualContainerGroups`` is a tree: ``{group: {isHidden, children: {...}}}``."""
    if not isinstance(groups, dict) or depth > 8:
        return
    for group_id, state in groups.items():
        if isinstance(state, dict):
            found[str(group_id)] = state.get("isHidden") is True
            _group_states(state.get("children"), found, depth + 1)


def bookmark_display(bookmark: Bookmark, page: ImportedPage) -> dict[str, bool]:
    """{visual or group id: hidden} for what the bookmark shows and hides on ``page``.

    A visual the bookmark captured without a display mode is shown. A bookmark of the whole
    page also shows the visuals it does not list (Desktop applies it clearing what it did
    not capture); a "selected visuals" bookmark leaves the others alone. Nothing changes
    when the bookmark does not capture display or has no state for this page.
    """
    section = bookmark.section(page.id)
    if not bookmark.captures["display"] or not section:
        return {}
    ids = {v.id for v in page.visuals}
    targets = bookmark.targets
    found: dict[str, bool] = {}
    if targets is None:
        found = {v.id: False for v in page.visuals if v.container_kind == "visual"}
    for visual_id, container in bookmark.containers(page.id).items():
        if visual_id in ids:
            found[visual_id] = bool(_hidden_of(container))
    groups: dict[str, bool] = {}
    _group_states(section.get("visualContainerGroups"), groups)
    for group_id, hidden in groups.items():
        if group_id in ids and (targets is None or group_id in targets):
            found[group_id] = hidden
    return found


def _saved_general(container: Json) -> list[Any]:
    """A slicer's selection as a bookmark saved it: Desktop's serializer writes it under
    ``singleVisual.objects.merge.general``; a plain ``objects.general`` is read too."""
    single = container.get("singleVisual")
    objects = single.get("objects") if isinstance(single, dict) else None
    if not isinstance(objects, dict):
        return []
    merge = objects.get("merge")
    general = merge.get("general") if isinstance(merge, dict) else objects.get("general")
    return list(general) if isinstance(general, list) else []


def bookmark_slicers(bookmark: Bookmark, page: ImportedPage) -> dict[str, list[Any]]:
    """{slicer id: its ``general`` entries as the bookmark saved them} ([] = cleared).

    A bookmark of the whole page clears the slicers it did not capture, as Desktop does."""
    section = bookmark.section(page.id)
    if not bookmark.captures["data"] or not section:
        return {}
    slicers = {v.id for v in page.visuals if v.visual_type in {"slicer", "advancedSlicerVisual"}}
    containers = bookmark.containers(page.id)
    found: dict[str, list[Any]] = {}
    for visual_id in sorted(slicers):
        if visual_id in containers:
            found[visual_id] = _saved_general(containers[visual_id])
        elif bookmark.targets is None:
            found[visual_id] = []
    return found


# Where Desktop's capture puts a filter's state (the navigation family writes the same): a
# filter hidden or locked in view mode by its name, one on a field by its expression, an
# include or exclude filter by its type.
SAVED_FILTER_KINDS = ("byName", "byExpr", "byType")


def _saved_states(holder: Any) -> tuple[list[Json] | None, bool]:
    """The filter states a bookmark saved in one scope (None: it saved none there), and
    whether some state could not be read. ``byName`` is ``{name: state}`` (a list is read
    too), ``byExpr`` and ``byType`` are lists; a state is ``{name, type?, filter?,
    expression?, howCreated?}``."""
    filters = holder.get("filters") if isinstance(holder, dict) else None
    if not isinstance(filters, dict) or not any(k in filters for k in SAVED_FILTER_KINDS):
        return None, False
    found: list[Json] = []
    unreadable = False
    for key in SAVED_FILTER_KINDS:
        raw = filters.get(key)
        if raw is None:
            continue
        items: list[Any] | None
        if key == "byName" and isinstance(raw, dict):
            items = [
                {"name": name, **state}
                if isinstance(state, dict) and "name" not in state
                else state
                for name, state in raw.items()
            ]
        else:
            items = raw if isinstance(raw, list) else None
        if items is None:
            unreadable = True
            continue
        for item in items:
            if isinstance(item, dict):
                found.append(item)
            else:
                unreadable = True
    return found, unreadable


def bookmark_filters(bookmark: Bookmark, page: ImportedPage) -> Json:
    """The filter states the bookmark saved: ``{report, page, visuals: {id: [...]},
    unreadable: {report, page, visuals: [id]}}``, each a list of filter states (``name``,
    ``filter``, ``expression``) or None where it saved none; ``unreadable`` says where a saved
    state could not be read (what it filters is not known, so the values are approximate)."""
    if not bookmark.captures["data"]:
        return {
            "report": None,
            "page": None,
            "visuals": {},
            "unreadable": {"report": False, "page": False, "visuals": []},
        }
    visuals: dict[str, list[Json]] = {}
    unreadable_visuals: list[str] = []
    for visual_id, container in bookmark.containers(page.id).items():
        found, unreadable = _saved_states(container)
        if found is not None:
            visuals[visual_id] = found
        if unreadable:
            unreadable_visuals.append(visual_id)
    report, report_unreadable = _saved_states(_exploration(bookmark.body))
    section, page_unreadable = _saved_states(bookmark.section(page.id))
    return {
        "report": report,
        "page": section,
        "visuals": visuals,
        "unreadable": {
            "report": report_unreadable,
            "page": page_unreadable,
            "visuals": unreadable_visuals,
        },
    }


def replace_filters(config: Any, saved: list[Json] | None) -> tuple[Json, list[str], int]:
    """A copy of a ``filterConfig`` with the bookmark's saved filter states applied.

    A state is matched by its ``name``; one without a name by its ``expression`` (the filter
    on the same field, when exactly one is). A state without ``filter`` clears that filter.
    Returns the names the bookmark saved that the report no longer has (Power BI cannot
    apply them either) and how many states Studio could not apply (no name and no single
    filter on their field, or a filter that is not an object): those leave the values
    approximate.
    """
    base: Json = json.loads(json.dumps(config)) if isinstance(config, dict) else {}
    if saved is None:
        return base, [], 0
    filters = base.get("filters")
    current = [f for f in filters if isinstance(f, dict)] if isinstance(filters, list) else []
    by_name = {f.get("name"): f for f in current if isinstance(f.get("name"), str)}
    missing: list[str] = []
    unapplied = 0
    for entry in saved:
        name = entry.get("name")
        state = entry.get("filter")
        if state is not None and not isinstance(state, dict):
            unapplied += 1
            continue
        if isinstance(name, str):
            target = by_name.get(name)
            if target is None:
                missing.append(name)
                continue
        else:
            expression = entry.get("expression")
            same = [f for f in current if expression is not None and f.get("field") == expression]
            if len(same) != 1:
                unapplied += 1
                continue
            target = same[0]
        if isinstance(state, dict):
            target["filter"] = json.loads(json.dumps(state))
        else:
            target.pop("filter", None)
    base["filters"] = current
    return base, missing, unapplied


def bookmarks_payload(bookmarks: Sequence[Bookmark], pages: Sequence[ImportedPage]) -> list[Json]:
    """The bookmark list the canvas offers, in pane order."""
    by_id = {p.id: p for p in pages}
    found: list[Json] = []
    for bookmark in bookmarks:
        page = by_id.get(bookmark.page or "")
        display = bookmark_display(bookmark, page) if page is not None else {}
        found.append(
            {
                "name": bookmark.name,
                "displayName": bookmark.display_name,
                "group": {"name": bookmark.group[0], "displayName": bookmark.group[1]}
                if bookmark.group
                else None,
                "page": page.id if page is not None else None,
                "targets": bookmark.targets,
                "captures": bookmark.captures,
                "hidden": sorted(k for k, v in display.items() if v),
                "shown": sorted(k for k, v in display.items() if not v),
            }
        )
    return found


# --- actions, buttons and navigators -----------------------------------------------------

_WEB = re.compile(r"https?://[^\s<>\"']{1,2000}", re.IGNORECASE)
_ACTION_TYPES = {
    "back": "back",
    "bookmark": "bookmark",
    "pagenavigation": "page",
    "drillthrough": "drillthrough",
    "weburl": "url",
    "qna": "qna",
}


def action_of(
    body: Mapping[str, Any], pages: Sequence[ImportedPage], bookmark_names: Iterable[str]
) -> Json | None:
    """What clicking the visual does (its ``visualLink``), or None when it does nothing.

    ``type`` is ``back``, ``bookmark``, ``page``, ``drillthrough``, ``url``, ``qna`` or
    ``unknown``; ``target`` is the page or bookmark name, ``valid`` says it exists. A web
    address is passed only when it is http(s); Studio never opens it by itself.
    """
    holder = body.get("visualContainerObjects") or body.get("vcObjects")
    link = properties(holder if isinstance(holder, dict) else {}, "visualLink")
    raw_type = pbir_expr.decode(link.get("type"))
    if not isinstance(raw_type, str) or pbir_expr.decode(link.get("show")) is False:
        return None
    kind = _ACTION_TYPES.get(raw_type.replace(" ", "").casefold(), "unknown")
    page_ids = {p.id for p in pages}
    target: str | None = None
    valid = True
    if kind == "bookmark":
        found = pbir_expr.decode(link.get("bookmark"))
        target = found if isinstance(found, str) else None
        valid = target is not None and target in set(bookmark_names)
    elif kind in {"page", "drillthrough"}:
        key = "navigationSection" if kind == "page" else "drillthroughSection"
        found = pbir_expr.decode(link.get(key))
        target = found if isinstance(found, str) else None
        valid = target is not None and target in page_ids
    action: Json = {"type": kind, "target": target, "valid": valid}
    if kind == "url":
        found = pbir_expr.decode(link.get("webUrl"))
        action["url"] = found if isinstance(found, str) and _WEB.fullmatch(found) else None
        action["valid"] = action["url"] is not None
    tooltip = pbir_expr.decode(link.get("tooltip"))
    action["tooltip"] = tooltip[:300] if isinstance(tooltip, str) else None
    return action


def navigator_items(
    visual: ImportedVisual,
    pages: Sequence[ImportedPage],
    bookmarks: Sequence[Bookmark],
    current_page: str,
    current_bookmark: str | None,
) -> list[Json]:
    """The buttons of a page or bookmark navigator, in the order Desktop draws them."""
    body = visual.definition.get("visual")
    objects = body.get("objects") if isinstance(body, dict) else None
    objects = objects if isinstance(objects, dict) else {}
    if visual.visual_type == "pageNavigator":
        settings = properties(objects, "pages")
        hidden_ok = pbir_expr.decode(settings.get("showHiddenPages")) is True
        tooltip_ok = pbir_expr.decode(settings.get("showTooltipPages")) is True
        default = pbir_expr.decode(settings.get("showByDefault")) is not False
        items: list[Json] = []
        for page in sorted(pages, key=lambda p: p.order):
            own = properties(objects, "pages", identity=page.id).get("showPage")
            shown = pbir_expr.decode(own) if own is not None else None
            if shown is False or (shown is None and not default):
                continue
            if shown is None and (
                (page.hidden and not hidden_ok) or (page.page_type == "tooltip" and not tooltip_ok)
            ):
                continue
            items.append(
                {
                    "label": page.title,
                    "action": {"type": "page", "target": page.id, "valid": True},
                    "selected": page.id == current_page,
                }
            )
        return items
    settings = properties(objects, "bookmarks")
    group = pbir_expr.decode(settings.get("bookmarkGroup"))
    chosen = pbir_expr.decode(settings.get("selectedBookmark"))
    selected = current_bookmark or (chosen if isinstance(chosen, str) else None)
    found: list[Json] = []
    for bookmark in bookmarks:
        if (
            isinstance(group, str)
            and group
            and (bookmark.group is None or group not in bookmark.group)
        ):
            continue
        found.append(
            {
                "label": bookmark.display_name,
                "action": {"type": "bookmark", "target": bookmark.name, "valid": True},
                "selected": selected in {bookmark.name, bookmark.display_name},
            }
        )
    return found


# --- pages, tooltips, interactions, sync groups, mobile ----------------------------------


def field_name(node: Any) -> Json | None:
    """``{field: "Table.Column", table, column}`` of a bound field expression, or None."""
    column = node.get("Column") if isinstance(node, dict) else None
    source = column.get("Expression", {}).get("SourceRef", {}) if isinstance(column, dict) else {}
    table = source.get("Entity") if isinstance(source, dict) else None
    name = column.get("Property") if isinstance(column, dict) else None
    if isinstance(table, str) and isinstance(name, str):
        return {"field": f"{table}.{name}", "table": table, "column": name}
    return None


def page_binding(page_config: Mapping[str, Any]) -> Json | None:
    """A drillthrough or tooltip page's bound fields: ``{type, fields: [{field, table,
    column}], keepAllFilters, crossReport}``, None for an ordinary page. ``keepAllFilters``
    is Desktop's "Keep all filters" (``acceptsFilterContext`` is ``None`` when it is off, on
    by default); ``crossReport`` is a drillthrough other reports may target
    (``referenceScope: CrossReport``)."""
    binding = page_config.get("pageBinding")
    if not isinstance(binding, dict):
        return None
    kind = str(binding.get("type") or page_config.get("type") or "").casefold()
    if kind not in {"drillthrough", "tooltip"}:
        return None
    parameters = binding.get("parameters")
    fields = [
        found
        for p in (parameters if isinstance(parameters, list) else [])
        if isinstance(p, dict) and (found := field_name(p.get("fieldExpr"))) is not None
    ]
    return {
        "type": kind,
        "fields": fields,
        "keepAllFilters": str(binding.get("acceptsFilterContext", "")).casefold() != "none",
        "crossReport": str(binding.get("referenceScope", "")).casefold() == "crossreport",
    }


def drillthroughs(
    pages: Sequence[ImportedPage], page_configs: Mapping[str, Mapping[str, Any]]
) -> list[Json]:
    """The report's drillthrough pages, in page order: what a "Drill through" menu offers
    for a data point whose fields include one of a page's ``fields``."""
    found: list[Json] = []
    for page in sorted(pages, key=lambda p: p.order):
        binding = page_binding(page_configs.get(page.id) or {})
        if binding is None or binding["type"] != "drillthrough":
            continue
        found.append(
            {
                "page": page.id,
                "title": page.title,
                "fields": binding["fields"],
                "keepAllFilters": binding["keepAllFilters"],
                "crossReport": binding["crossReport"],
            }
        )
    return found


def tooltip_page(body: Mapping[str, Any], pages: Sequence[ImportedPage]) -> Json | None:
    """The report page a visual shows as its tooltip (``visualTooltip`` of a page type)."""
    holder = body.get("visualContainerObjects") or body.get("vcObjects")
    tip = properties(holder if isinstance(holder, dict) else {}, "visualTooltip")
    if pbir_expr.decode(tip.get("show")) is False:
        return None
    kind = pbir_expr.decode(tip.get("type"))
    section = pbir_expr.decode(tip.get("section"))
    # Desktop writes a report-page tooltip as type 'Canvas'; 'ReportPage' is read too.
    if not isinstance(kind, str) or kind.casefold() not in {"canvas", "reportpage"}:
        return None
    if not isinstance(section, str):
        return None
    page = next((p for p in pages if p.id == section), None)
    if page is None:
        return None
    return {"page": page.id, "title": page.title, "width": page.width, "height": page.height}


def interaction(
    source: ImportedVisual,
    source_kind: str,
    target: ImportedVisual,
    explicit: Sequence[Any],
    filter_by_default: bool,
) -> str:
    """How ``target`` reacts to a selection in ``source``: ``filter``, ``highlight``,
    ``none`` or ``unsupported`` (a type Studio does not know).

    Desktop's default is to highlight a target that can (column, bar, pie and donut charts)
    and to filter everything else; a slicer always filters; the report setting "cross-filter
    by default" (``defaultFilterActionIsDataFilter``) makes filtering the default.
    """
    kind = next(
        (
            i.get("type")
            for i in explicit
            if isinstance(i, dict) and i.get("source") == source.id and i.get("target") == target.id
        ),
        None,
    )
    if kind in (None, "Default"):
        if source_kind == "slicer" or filter_by_default:
            return "filter"
        return "highlight" if target.visual_type in HIGHLIGHT_TYPES else "filter"
    resolved = INTERACTION_TYPES.get(str(kind))
    if resolved == "highlight" and (
        source_kind == "slicer" or target.visual_type not in HIGHLIGHT_TYPES
    ):
        return "filter"
    return resolved or "unsupported"


def sync_group(body: Mapping[str, Any]) -> Json | None:
    """A slicer's sync group: slicers of one group share their selection across pages."""
    found = body.get("syncGroup")
    if not isinstance(found, dict) or not isinstance(found.get("groupName"), str):
        return None
    return {
        "groupName": found["groupName"][:200],
        "fieldChanges": found.get("fieldChanges") is not False,
        "filterChanges": found.get("filterChanges") is not False,
    }


def sync_groups(pages: Sequence[ImportedPage]) -> dict[str, list[Json]]:
    """Every sync group of the report: ``{group: [{page, visual, hidden}]}``."""
    groups: dict[str, list[Json]] = {}
    for page in pages:
        for visual in page.visuals:
            body = visual.definition.get("visual")
            found = sync_group(body) if isinstance(body, dict) else None
            if found is not None:
                groups.setdefault(found["groupName"], []).append(
                    {"page": page.id, "visual": visual.id, "hidden": visual.hidden}
                )
    return groups


def mobile_layout(z: zipfile.ZipFile, page: ImportedPage) -> Json | None:
    """The page's phone layout: ``{visuals: {id: {x, y, width, height, z}}}``, None when no
    visual of the page is placed on it."""
    found: dict[str, Json] = {}
    for visual in page.visuals:
        body = _read_json(z, f"Report/definition/pages/{page.id}/visuals/{visual.id}/mobile.json")
        position = body.get("position") if body else None
        if not isinstance(position, dict):
            continue
        box: Json = {}
        for key in ("x", "y", "width", "height", "z"):
            value = position.get(key, 0)
            box[key] = (
                float(max(0.0, min(float(value), 20000.0)))
                if isinstance(value, int | float) and not isinstance(value, bool)
                else 0.0
            )
        found[visual.id] = box
    return {"visuals": found} if found else None
