"""Structural checks on an edited document, run before a revision is committed.

``validate_document`` returns plain-English blocking problems; an empty list means the document
is valid. ``validate_findings`` is the same checks as typed ``Finding`` records (a stable
``code``, a severity, the part and pointer it is about), so a caller can tell a problem an edit
introduced from one the source report already had:

- ``capture_baseline`` records the findings of the head before a batch runs; ``baseline=`` then
  drops those from the answer. A finding is keyed by ``(code, part relative to definition/,
  pointer)``, never by its message, which embeds current values. A duplicated visual or page
  inherits its source's baseline through ``lineage`` (new name -> source name, for visuals and
  pages), because it is a copy that starts with the same defects.
- Blocking findings mean Power BI Desktop cannot open the file or would show something broken:
  a part without ``$schema`` or its required keys, duplicate or mismatched names, ``pageOrder``
  and ``activePageName`` that disagree with the pages, references that no longer resolve
  (selectors, bindings, sorts and filters against the model, interactions, bookmarks, group
  parents, page links, bound filters, and the navigation references
  ``report_refs.navigation_findings`` checks: the bookmark list, buttons' bookmarks,
  navigators, page types of links), filter names that are not unique report-wide, a filter that
  breaks Microsoft's filter schema, a literal Studio wrote whose type disagrees with the catalog,
  and positions that are not finite or lie outside the page.
- Advisory findings (overlap, minimum size, tab order, visual types Studio's canvas cannot draw,
  a file format newer than the ones Studio has checked) are for the user to read and never block.

What is and is not checked against Microsoft's schemas: filter objects are validated with the
published filterConfiguration 1.3.0 and semanticQuery 1.4.0 schemas (``pbir_schemas/``, run with
``jsonschema``; when the library or the files are missing that one check is skipped). No offline
copy of the visualContainer 2.12.0, page 2.1.0 or report 3.3.0 schemas exists, so their rules are
written by hand from the published 2.7.0 visualContainer schema and from files Desktop wrote.
Nothing here uses the network.

Literals are checked only where this batch changed them: the document's dirty parts, and inside
those only the entries that differ from the file as loaded, so a Desktop-written spelling that
disagrees with the catalog is never blamed on an edit. A visual outside its page is only blocking
when it is further out than it was in the file as loaded (a copy is compared with its source):
real reports often have a visual a few pixels over an edge, and that must not stop every later
edit, with or without a baseline. The right and bottom edges are also only held against a visual
(or a group around it) this batch changed, so shrinking a page does not blame its other visuals.
A pixel of slack absorbs Desktop's rounding.

Positions are page coordinates: a group's members store x and y relative to their group (nested
groups add up), so the group origins are added before comparing with the page.
"""

from __future__ import annotations

import importlib
import json
import math
import re
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from . import pbir_expr as expr
from .pbir_document import BOOKMARKS_META, ROOT, PageDoc, PbirDocument
from .pbix_package import PackageError, decode_json
from .report_edit_context import ModelInfo
from .report_refs import _scan, navigation_findings

Severity = Literal["blocking", "advisory"]
FindingKey = tuple[str, str, str]
# What ``capture_baseline`` returns and ``validate_findings`` takes: the keys of known findings.
Baseline = Collection[FindingKey]

# How far past the page edge a visual may run before it is a problem: Desktop writes positions
# with 14 digits, and a visual snapped to the edge can round over it.
_EDGE_TOLERANCE = 1.0
_MIN_SIZE = 20.0
_OVERLAP_SHARE = 0.25
_MAX_OVERLAP_VISUALS = 300
_MAX_NAME = 50


@dataclass(frozen=True)
class Finding:
    """One thing wrong (or worth a look) in the document."""

    code: str
    severity: Severity
    part: str  # the part name, e.g. Report/definition/pages/<page>/visuals/<visual>/visual.json
    pointer: str  # a JSON pointer inside the part ("" for the whole part)
    message: str  # plain English for the user
    # The pointer with every list position replaced by what identifies the entry (a filter's
    # name, a projection's queryRef...), so that removing an earlier entry does not move an
    # old defect to a new key. Empty when the finding was made without the part's data.
    stable: str = field(default="", compare=False, repr=False)

    @property
    def key(self) -> FindingKey:
        """What identifies the finding across edits: code, part below definition/, pointer."""
        return (self.code, _relative(self.part), self.stable or self.pointer)


def _relative(part: str) -> str:
    return part[len(ROOT) :] if part.startswith(ROOT) else part


def _pointer(*path: Any) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _identity(item: Any) -> str | None:
    """What tells a list entry apart from its neighbours besides its position, or None when
    only the position does (the operands of a condition, say)."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return None
    for key in ("name", "queryRef", "boundFilter"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return value
    if isinstance(item.get("source"), str) and isinstance(item.get("target"), str):
        return f"{item['source']}>{item['target']}"
    if "selector" in item:  # a formatting entry is known by the selector it applies to
        return json.dumps(item["selector"], sort_keys=True)
    return None


def _stable_pointer(data: Any, pointer: str) -> str:
    """``pointer`` with each list position swapped for the entry's identity (``@name``)."""
    if not pointer:
        return pointer
    node = data
    out: list[str] = []
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
            identity = _identity(node)
            out.append(token if identity is None else "@" + identity)
        else:
            node = node.get(token) if isinstance(node, dict) else None
            out.append(token)
    return _pointer(*out)


# --- schema versions ------------------------------------------------------------------------

# The versions Studio was built and verified against. A part at a newer version may have rules
# Studio does not know, so it gets an advisory. What keeps Studio to catalog-known edits there is
# not this finding but ``_check_literals``, which holds every entry a batch writes to a changed
# visual to the catalog (unknown objects and properties block, whatever the part's version).
_PINNED_SCHEMAS = {
    "visualContainer": (2, 12, 0),
    "page": (2, 1, 0),
    "report": (3, 3, 0),
    "pagesMetadata": (1, 1, 0),
}
_SCHEMA_URL = re.compile(r"/definition/([A-Za-z]+)/(\d+)\.(\d+)\.(\d+)/schema\.json$")


def _schema_finding(part: str, data: dict[str, Any]) -> Finding | None:
    schema = data.get("$schema")
    if not isinstance(schema, str):
        return None
    found = _SCHEMA_URL.search(schema)
    if not found:
        return None
    kind, version = found.group(1), tuple(int(found.group(i)) for i in (2, 3, 4))
    pinned = _PINNED_SCHEMAS.get(kind)
    if pinned is None or (version[0] == pinned[0] and version <= pinned):
        return None
    return Finding(
        "schema_version_unknown",
        "advisory",
        part,
        _pointer("$schema"),
        f"This report uses a newer file format ({kind} {'.'.join(map(str, version))}) than "
        "Studio has checked, so Studio only makes changes it has verified.",
    )


# --- the document's parts -------------------------------------------------------------------


def _visual_part(page: PageDoc, visual_id: str) -> str:
    return f"{page.folder}visuals/{visual_id}/visual.json"


def _page_part(page: PageDoc) -> str:
    return page.folder + "page.json"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _page_size(page: PageDoc) -> tuple[float, float]:
    width, height = _number(page.data.get("width")), _number(page.data.get("height"))
    return (width if width and width > 0 else 1280.0, height if height and height > 0 else 720.0)


def _page_rect(page: PageDoc, visual_id: str) -> tuple[float, float, float, float] | None:
    """(x, y, width, height) of a visual in page coordinates, or None when a position is
    unreadable. A member's own x and y are relative to its group, and a group's to its own."""
    x = y = 0.0
    size: tuple[float, float] | None = None
    name: str | None = visual_id
    seen: set[str] = set()
    while name is not None and name in page.visuals and name not in seen:
        seen.add(name)
        position = page.visuals[name].get("position")
        if not isinstance(position, dict):
            return None
        px, py = _number(position.get("x")), _number(position.get("y"))
        if px is None or py is None:
            return None
        x, y = x + px, y + py
        if size is None:
            w, h = _number(position.get("width")), _number(position.get("height"))
            if w is None or h is None:
                return None
            size = (w, h)
        parent = page.visuals[name].get("parentGroupName")
        name = parent if isinstance(parent, str) else None
    if size is None:
        return None
    return x, y, size[0], size[1]


def _changed_parts(document: PbirDocument) -> set[str]:
    """Parts this batch changed (the document's dirty set; empty once it was committed)."""
    dirty = getattr(document, "_dirty", None)
    return set(dirty) if isinstance(dirty, set) else set()


# --- the checks -----------------------------------------------------------------------------


class _Run:
    def __init__(
        self,
        document: PbirDocument,
        model: ModelInfo | None,
        lineage: Mapping[str, str] | None = None,
    ) -> None:
        self.document = document
        # A model with nothing in it (a thin report, or none given) cannot say a field is missing.
        self.model = model if model is not None and (model.columns or model.measures) else None
        self.findings: list[Finding] = []
        self.report_measures = _extension_measures(document)
        self.roots = _roots(lineage or {})
        self._parts: dict[str, Any] | None = None
        self._original_parts: dict[str, str] | None = None
        self._original: dict[str, dict[str, Any]] = {}

    def add(self, code: str, severity: Severity, part: str, pointer: str, message: str) -> None:
        stable = _stable_pointer(self._data_of(part), pointer)
        self.findings.append(Finding(code, severity, part, pointer, message, stable))

    def _data_of(self, part: str) -> Any:
        """The loaded JSON of a part of the document, or None for a part it does not hold."""
        if self._parts is None:
            document = self.document
            parts: dict[str, Any] = {ROOT + "report.json": document.report}
            parts[ROOT + "pages/pages.json"] = document.pages_meta
            parts[BOOKMARKS_META] = document.bookmarks_metadata
            for page in document.pages.values():
                parts[_page_part(page)] = page.data
                for visual_id, visual in page.visuals.items():
                    parts[_visual_part(page, visual_id)] = visual
            self._parts = parts
        found = self._parts.get(part)
        prefix, suffix = ROOT + "bookmarks/", ".bookmark.json"
        if found is None and part.startswith(prefix) and part.endswith(suffix):
            found = self.document.bookmarks().get(part[len(prefix) : -len(suffix)])
        return found

    def original_visual(self, name: str) -> dict[str, Any] | None:
        """A visual as the document was loaded (the head before the batch), by name."""
        if self._original_parts is None:
            found = re.compile(re.escape(ROOT) + r"pages/[^/]+/visuals/([^/]+)/visual\.json")
            self._original_parts = {}
            for entry in self.document.package.names():
                match = found.fullmatch(entry)
                if match:
                    self._original_parts.setdefault(match.group(1), entry)
        if name not in self._original:
            part = self._original_parts.get(name)
            self._original[name] = _original_visual(self.document, part) if part else {}
        return self._original[name] or None

    def original_rect(
        self, visual_id: str
    ) -> tuple[tuple[float, float, float, float], tuple[float, float]] | None:
        """``_page_rect`` of the visual in the file as loaded, with the size of the page it was
        on there. A copy the batch made is measured as its source was (``lineage``); a visual
        the batch created has no original."""
        x = y = 0.0
        size: tuple[float, float] | None = None
        page_size = (1280.0, 720.0)
        name: str | None = self.roots.get(visual_id, visual_id)
        seen: set[str] = set()
        while name is not None and name not in seen:
            seen.add(name)
            data = self.original_visual(name)
            position = data.get("position") if data else None
            if not isinstance(position, dict):
                return None
            px, py = _number(position.get("x")), _number(position.get("y"))
            if px is None or py is None:
                return None
            x, y = x + px, y + py
            if size is None:
                w, h = _number(position.get("width")), _number(position.get("height"))
                if w is None or h is None:
                    return None
                size = (w, h)
                page_size = self._original_page_size(name)
            parent = data.get("parentGroupName") if data else None
            name = parent if isinstance(parent, str) else None
        return None if size is None else ((x, y, size[0], size[1]), page_size)

    def _original_page_size(self, visual_name: str) -> tuple[float, float]:
        part = (self._original_parts or {}).get(visual_name, "")
        folder = part.rsplit("/visuals/", 1)[0]
        try:
            data = decode_json(self.document.package.read(folder + "/page.json"))
        except (KeyError, PackageError, ValueError):
            return 1280.0, 720.0
        width = _number(data.get("width")) if isinstance(data, dict) else None
        height = _number(data.get("height")) if isinstance(data, dict) else None
        return (
            width if width and width > 0 else 1280.0,
            height if height and height > 0 else 720.0,
        )

    def blocking(self, code: str, part: str, pointer: str, message: str) -> None:
        self.add(code, "blocking", part, pointer, message)

    def advisory(self, code: str, part: str, pointer: str, message: str) -> None:
        self.add(code, "advisory", part, pointer, message)


def _roots(lineage: Mapping[str, str]) -> dict[str, str]:
    """Each copy's name -> the name of the original at the end of its chain (a copy of a copy
    is a copy of the first source)."""
    roots: dict[str, str] = {}
    for name in lineage:
        current, seen = name, {name}
        while current in lineage and lineage[current] not in seen:
            current = lineage[current]
            seen.add(current)
        roots[name] = current
    return roots


def _extension_measures(document: PbirDocument) -> set[tuple[str, str]]:
    """(table, measure) of the report-level measures of a thin report, lower-cased."""
    found: set[tuple[str, str]] = set()
    entities = document.extensions.get("entities")
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, dict):
            continue
        for measure in entity.get("measures") or []:
            if isinstance(measure, dict) and isinstance(measure.get("name"), str):
                found.add((str(entity.get("name", "")).casefold(), measure["name"].casefold()))
    return found


def _check_report_and_pages(run: _Run) -> None:
    document = run.document
    part = ROOT + "report.json"
    if not isinstance(document.report.get("$schema"), str):
        run.blocking("schema_missing", part, _pointer("$schema"), "The report has no schema")
    finding = _schema_finding(part, document.report)
    if finding:
        run.findings.append(finding)
    pages_part = ROOT + "pages/pages.json"
    if document.package.has(pages_part):
        if not isinstance(document.pages_meta.get("$schema"), str):
            run.blocking(
                "schema_missing", pages_part, _pointer("$schema"), "The page list has no schema"
            )
        finding = _schema_finding(pages_part, document.pages_meta)
        if finding:
            run.findings.append(finding)
    order = document.pages_meta.get("pageOrder", [])
    order = [n for n in order if isinstance(n, str)] if isinstance(order, list) else []
    if set(order) != set(document.pages) or (
        isinstance(document.pages_meta.get("pageOrder"), list)
        and len(document.pages_meta["pageOrder"]) != len(order)
    ):
        run.blocking(
            "page_order_mismatch",
            pages_part,
            _pointer("pageOrder"),
            "pages.json lists a different set of pages",
        )
    elif len(order) != len(set(order)):
        run.blocking(
            "page_order_duplicate", pages_part, _pointer("pageOrder"), "A page is listed twice"
        )
    active = document.pages_meta.get("activePageName")
    if isinstance(active, str) and active not in document.pages:
        run.blocking(
            "active_page_missing",
            pages_part,
            _pointer("activePageName"),
            f"The report opens on page {active!r}, which does not exist",
        )
    seen_pages: set[str] = set()
    for page in document.pages.values():
        _check_page(run, page, seen_pages)


def _check_page(run: _Run, page: PageDoc, seen: set[str]) -> None:
    part = _page_part(page)
    data = page.data
    if not isinstance(data.get("$schema"), str):
        run.blocking("schema_missing", part, _pointer("$schema"), f"Page {page.name} has no schema")
    finding = _schema_finding(part, data)
    if finding:
        run.findings.append(finding)
    name = data.get("name")
    if not isinstance(name, str) or not name:
        run.blocking("required_missing", part, _pointer("name"), f"Page {page.name} has no name")
    elif name != page.name:
        run.blocking(
            "page_name_mismatch", part, _pointer("name"), f"Page {page.name} is named {name!r}"
        )
    elif name in seen:
        run.blocking(
            "page_name_duplicate", part, _pointer("name"), f"Two pages share the name {name}"
        )
    else:
        seen.add(name)
    if not isinstance(data.get("displayName"), str) or not data.get("displayName"):
        run.blocking(
            "required_missing", part, _pointer("displayName"), f"Page {page.name} has no title"
        )
    for key in ("width", "height"):
        if key in data and (_number(data[key]) is None or float(data[key]) <= 0):
            run.blocking(
                "page_size_invalid",
                part,
                _pointer(key),
                f"Page {page.display_name} has a {key} that is not a positive number",
            )
    _check_interactions(run, page)
    _check_bound_filters(run, page)


def _check_interactions(run: _Run, page: PageDoc) -> None:
    items = page.data.get("visualInteractions")
    for index, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        for role in ("source", "target"):
            name = item.get(role)
            if isinstance(name, str) and name not in page.visuals:
                run.blocking(
                    "interaction_visual_missing",
                    _page_part(page),
                    _pointer("visualInteractions", index, role),
                    f"An interaction on page {page.display_name} names a visual that is not there",
                )


def _check_bound_filters(run: _Run, page: PageDoc) -> None:
    binding = page.data.get("pageBinding")
    if not isinstance(binding, dict):
        return
    names = _filter_names_of(page.data)
    parameters = binding.get("parameters")
    for index, parameter in enumerate(parameters if isinstance(parameters, list) else []):
        bound = parameter.get("boundFilter") if isinstance(parameter, dict) else None
        if isinstance(bound, str) and bound not in names:
            run.blocking(
                "bound_filter_missing",
                _page_part(page),
                _pointer("pageBinding", "parameters", index, "boundFilter"),
                f"Page {page.display_name} is tied to a filter that is not on the page",
            )


def _filter_names_of(holder: dict[str, Any]) -> list[str]:
    config = holder.get("filterConfig")
    filters = config.get("filters") if isinstance(config, dict) else None
    if not isinstance(filters, list):
        return []
    return [f["name"] for f in filters if isinstance(f, dict) and isinstance(f.get("name"), str)]


# --- visuals --------------------------------------------------------------------------------


def _check_visual_structure(
    run: _Run, page: PageDoc, visual_id: str, visual: dict[str, Any]
) -> None:
    part = _visual_part(page, visual_id)
    if not isinstance(visual.get("$schema"), str):
        run.blocking(
            "schema_missing", part, _pointer("$schema"), f"Visual {visual_id} has no schema"
        )
    finding = _schema_finding(part, visual)
    if finding:
        run.findings.append(finding)
    name = visual.get("name")
    if name != visual_id:
        run.blocking(
            "visual_name_mismatch", part, _pointer("name"), f"Visual {visual_id} is named {name!r}"
        )
    elif len(visual_id) > _MAX_NAME:
        run.blocking(
            "visual_name_invalid",
            part,
            _pointer("name"),
            f"Visual {visual_id} has a name longer than {_MAX_NAME} characters",
        )
    body, group = visual.get("visual"), visual.get("visualGroup")
    if (body is None) == (group is None):
        run.blocking(
            "visual_kind_invalid",
            part,
            "",
            f"Visual {visual_id} must be either a visual or a group, not both or neither",
        )
    if isinstance(body, dict) and not isinstance(body.get("visualType"), str):
        run.blocking(
            "required_missing",
            part,
            _pointer("visual", "visualType"),
            f"Visual {visual_id} has no visual type",
        )
    if isinstance(group, dict):
        for key in ("displayName", "groupMode"):
            if not isinstance(group.get(key), str):
                run.blocking(
                    "required_missing",
                    part,
                    _pointer("visualGroup", key),
                    f"The group {visual_id} has no {'name' if key == 'displayName' else 'mode'}",
                )
    position = visual.get("position")
    if not isinstance(position, dict):
        run.blocking(
            "required_missing", part, _pointer("position"), f"Visual {visual_id} has no position"
        )


def _touched(page: PageDoc, visual_id: str, changed: set[str]) -> bool:
    """This batch changed the visual, or a group it sits in (which moves it)."""
    seen: set[str] = set()
    name: str | None = visual_id
    while name is not None and name in page.visuals and name not in seen:
        seen.add(name)
        if _visual_part(page, name) in changed:
            return True
        parent = page.visuals[name].get("parentGroupName")
        name = parent if isinstance(parent, str) else None
    return False


def _check_visual_position(
    run: _Run, page: PageDoc, visual_id: str, visual: dict[str, Any], changed: set[str]
) -> None:
    part = _visual_part(page, visual_id)
    position = visual.get("position")
    if not isinstance(position, dict):
        return
    for key in ("x", "y", "width", "height"):
        if _number(position.get(key)) is None:
            run.blocking(
                "position_invalid",
                part,
                _pointer("position", key),
                f"Visual {visual_id} has a {key} that is not a number",
            )
            return
    if float(position["width"]) <= 0 or float(position["height"]) <= 0:
        run.blocking(
            "position_invalid",
            part,
            _pointer("position"),
            f"Visual {visual_id} has no size",
        )
        return
    rect = _page_rect(page, visual_id)
    if rect is None:
        return
    x, y, width, height = rect
    page_width, page_height = _page_size(page)
    now = (-x, -y, x + width - page_width, y + height - page_height)
    if max(now) <= _EDGE_TOLERANCE:
        return
    # A source report often has a visual over an edge; that is not this edit's to answer for,
    # so a side only counts when the visual is further out than the file it came from had it
    # (a copy is measured against its source).
    was = run.original_rect(visual_id)
    before = (0.0,) * 4
    if was is not None:
        (wx, wy, ww, wh), (was_width, was_height) = was
        before = (-wx, -wy, wx + ww - was_width, wy + wh - was_height)
    worse = [
        o > _EDGE_TOLERANCE and o > b + _EDGE_TOLERANCE for o, b in zip(now, before, strict=True)
    ]
    messages = (
        f"Visual {visual_id} is outside the page",
        f"Visual {visual_id} is outside the page",
        f"Visual {visual_id} runs past the right edge of the page",
        f"Visual {visual_id} runs past the bottom edge of the page",
    )
    for side, message in enumerate(messages):
        # Shrinking the page is not a visual's edit: the right and bottom edges are only held
        # against a visual (or a group around it) the batch changed.
        if worse[side] and (side < 2 or _touched(page, visual_id, changed)):
            run.blocking("position_outside_page", part, _pointer("position"), message)
            return


def _check_groups(run: _Run, page: PageDoc) -> None:
    for visual_id, visual in page.visuals.items():
        parent = visual.get("parentGroupName")
        if parent is None:
            continue
        part = _visual_part(page, visual_id)
        target = page.visuals.get(parent) if isinstance(parent, str) else None
        if target is None or "visualGroup" not in target:
            run.blocking(
                "group_parent_missing",
                part,
                _pointer("parentGroupName"),
                f"Visual {visual_id} is in a group that is not on the page",
            )
            continue
        chain, name = {visual_id}, parent
        while isinstance(name, str) and name in page.visuals:
            if name in chain:
                run.blocking(
                    "group_cycle",
                    part,
                    _pointer("parentGroupName"),
                    f"Visual {visual_id} is in groups that contain each other",
                )
                break
            chain.add(name)
            name = page.visuals[name].get("parentGroupName")


def _projection_refs(visual: dict[str, Any]) -> set[str]:
    body = visual.get("visual")
    query = body.get("query") if isinstance(body, dict) else None
    state = query.get("queryState") if isinstance(query, dict) else None
    refs: set[str] = set()
    for role in state.values() if isinstance(state, dict) else []:
        projections = role.get("projections") if isinstance(role, dict) else None
        for projection in projections if isinstance(projections, list) else []:
            if isinstance(projection, dict) and isinstance(projection.get("queryRef"), str):
                refs.add(projection["queryRef"])
    return refs


def _model_has_ref(model: ModelInfo | None, metadata: str) -> bool:
    """``Table.Field`` names a column or measure of the model."""
    if model is None:
        return False
    wanted = metadata.casefold()
    if any(f"{home}.{m}".casefold() == wanted for m, home in model.measures.items()):
        return True
    return any(
        f"{table}.{column}".casefold() == wanted
        for table, columns in model.columns.items()
        for column in columns
    )


def _check_selectors(run: _Run, page: PageDoc, visual_id: str, visual: dict[str, Any]) -> None:
    refs = _projection_refs(visual)
    body = visual.get("visual")
    if not isinstance(body, dict):
        return
    for where in ("objects", "visualContainerObjects"):
        objects = body.get(where)
        for object_name, entries in objects.items() if isinstance(objects, dict) else []:
            for index, entry in enumerate(entries if isinstance(entries, list) else []):
                selector = entry.get("selector") if isinstance(entry, dict) else None
                metadata = selector.get("metadata") if isinstance(selector, dict) else None
                if (
                    isinstance(metadata, str)
                    and metadata not in refs
                    and not re.match(r"^\w+\(", metadata)
                    and not _model_has_ref(run.model, metadata)
                ):
                    run.blocking(
                        "selector_field_missing",
                        _visual_part(page, visual_id),
                        _pointer("visual", where, object_name, index, "selector", "metadata"),
                        f"{visual_id}: {object_name} formats a field the visual lacks ({metadata})",
                    )


# Tables Desktop creates and hides for a date column's automatic date hierarchy: a report can
# bind to them although the import's table list may not carry them.
_AUTO_DATE_TABLES = ("LocalDateTable_", "DateTableTemplate_")


def _field_known(run: _Run, kind: str, entity: str, prop: str) -> bool:
    model = run.model
    if model is None or entity.startswith(_AUTO_DATE_TABLES):
        return True
    if kind == "Measure":
        home = model.measure_table(prop)
        if home is not None and home.casefold() == entity.casefold():
            return True
        return (entity.casefold(), prop.casefold()) in run.report_measures
    table = next((t for t in model.columns if t.casefold() == entity.casefold()), None)
    return table is not None and any(c.casefold() == prop.casefold() for c in model.columns[table])


def _field_code(path: list[Any]) -> str:
    if "filterConfig" in path:
        return "filter_field_missing"
    if "sortDefinition" in path:
        return "sort_field_missing"
    if "queryState" in path:
        return "binding_field_missing"
    return "expression_field_missing"


_FIELD_WORDS = {
    "filter_field_missing": "a filter",
    "sort_field_missing": "its sort",
    "binding_field_missing": "a field",
    "expression_field_missing": "a formatting rule",
}


def _check_fields(run: _Run, part: str, label: str, data: dict[str, Any]) -> None:
    """Every column and measure a part refers to must exist in the model, and a filter must
    reach its table through a From alias it declares (never through ``Entity`` in a Where)."""

    def sink(
        kind: str,
        body: dict[str, Any],
        holder: dict[str, Any],
        projection: dict[str, Any],
        path: list[Any],
        entity: str | None,
    ) -> None:
        source = body["Expression"].get("SourceRef")
        source = source if isinstance(source, dict) else {}
        in_filter = "filterConfig" in path
        if in_filter and "Source" in source and entity is None:
            run.blocking(
                "filter_source_unknown",
                part,
                _pointer(*path),
                f"A filter in {label} uses a table alias its query does not declare",
            )
            return
        if in_filter and "Where" in path and "Entity" in source:
            run.blocking(
                "filter_entity_in_where",
                part,
                _pointer(*path),
                f"A filter in {label} names its table directly instead of through the query's "
                "table list",
            )
        if entity is None or _field_known(run, kind, entity, str(body["Property"])):
            return
        code = _field_code(path)
        run.blocking(
            code,
            part,
            _pointer(*path),
            f"{label[:1].upper()}{label[1:]} uses {_FIELD_WORDS[code]}, "
            f"{entity}[{body['Property']}], that the model does not have",
        )

    _scan(data, [], [], None, sink)


def _check_links(run: _Run, page: PageDoc, visual_id: str, visual: dict[str, Any]) -> None:
    body = visual.get("visual")
    if not isinstance(body, dict):
        return
    for where in ("objects", "visualContainerObjects"):
        objects = body.get(where)
        if not isinstance(objects, dict):
            continue
        for object_name, props in (
            ("visualLink", ("navigationSection", "drillthroughSection")),
            ("visualTooltip", ("section",)),
        ):
            entries = objects.get(object_name)
            for index, entry in enumerate(entries if isinstance(entries, list) else []):
                properties = entry.get("properties") if isinstance(entry, dict) else None
                if not isinstance(properties, dict):
                    continue
                for prop in props:
                    target = expr.decode(properties.get(prop))
                    if isinstance(target, str) and target and target not in run.document.pages:
                        run.blocking(
                            "link_page_missing",
                            _visual_part(page, visual_id),
                            _pointer("visual", where, object_name, index, "properties", prop),
                            f"Visual {visual_id} links to a page that does not exist",
                        )


# --- filters --------------------------------------------------------------------------------


def _filter_holders(document: PbirDocument) -> Iterator[tuple[str, str, dict[str, Any]]]:
    yield ROOT + "report.json", "the report", document.report
    for page in document.pages.values():
        yield _page_part(page), f"page {page.display_name}", page.data
        for visual_id, visual in page.visuals.items():
            yield _visual_part(page, visual_id), f"visual {visual_id}", visual


def _check_filter_names(run: _Run) -> None:
    seen: dict[str, str] = {}
    for part, label, holder in _filter_holders(run.document):
        config = holder.get("filterConfig")
        filters = config.get("filters") if isinstance(config, dict) else None
        for index, item in enumerate(filters if isinstance(filters, list) else []):
            name = item.get("name") if isinstance(item, dict) else None
            if not isinstance(name, str):
                continue
            if name in seen:
                run.blocking(
                    "filter_name_duplicate",
                    part,
                    _pointer("filterConfig", "filters", index, "name"),
                    f"Two filters share the name {name} ({seen[name]} and {label})",
                )
            else:
                seen[name] = label


_SCHEMA_DIR = Path(__file__).with_name("pbir_schemas")
_SCHEMA_BASE = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"


@lru_cache(maxsize=1)
def _filter_validator() -> Any:
    """A jsonschema validator for ``filterConfig`` built from the two packaged schemas, or
    None when the library or the files are not there."""
    try:
        # Imported by name: jsonschema ships no type stubs (it comes with altair).
        jsonschema = importlib.import_module("jsonschema")
        referencing = importlib.import_module("referencing")
        draft7 = importlib.import_module("referencing.jsonschema").DRAFT7
        config = json.loads(
            (_SCHEMA_DIR / "filterConfiguration-1.3.0.embedded.json").read_text(encoding="utf-8")
        )
        query = json.loads((_SCHEMA_DIR / "semanticQuery-1.4.0.json").read_text(encoding="utf-8"))
    except (ImportError, OSError, ValueError):
        return None
    # filterConfiguration points at formattingObjectDefinitions for the selector of a filter
    # card's formatting; that schema was not cached and is only reached from there.
    stub = {
        "$id": _SCHEMA_BASE + "formattingObjectDefinitions/1.5.0/schema.json",
        "definitions": {"Selector": {}},
    }
    registry = referencing.Registry().with_resources(
        [
            (item["$id"], referencing.Resource.from_contents(item, default_specification=draft7))
            for item in (config, query, stub)
        ]
    )
    return jsonschema.Draft7Validator(config, registry=registry)


def _check_filter_schemas(run: _Run) -> None:
    validator = _filter_validator()
    if validator is None:
        return
    for part, label, holder in _filter_holders(run.document):
        config = holder.get("filterConfig")
        if not isinstance(config, dict):
            continue
        for error in sorted(validator.iter_errors(config), key=lambda e: list(e.absolute_path))[:5]:
            path = list(error.absolute_path)
            # Only a pointer into the filter and a short reason: the schema's own message can be
            # a page of alternatives.
            reason = str(error.message)[:100]
            run.blocking(
                "filter_schema",
                part,
                _pointer("filterConfig", *path),
                f"A filter in {label} is not in a form Power BI accepts ({reason})",
            )


# --- literals Studio wrote ------------------------------------------------------------------

_NUMERIC_SUBTYPES = {"fontSize", "labelDisplayUnits", "displayUnitsWithoutAuto"}


def _is_literal_node(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    inner = node.get("expr")
    return isinstance(inner, dict) and "Literal" in inner


def _literal_problem(spec: dict[str, Any], prop: str, node: Any) -> str | None:
    """What is wrong with a literal against the catalog type of its property, else None."""
    kind = spec.get("type")
    if kind == "fill":
        if not isinstance(node, dict) or "solid" not in node:
            return None
        color = node["solid"].get("color") if isinstance(node.get("solid"), dict) else None
        inner = color.get("expr") if isinstance(color, dict) else None
        themed = inner.get("ThemeDataColor") if isinstance(inner, dict) else None
        if isinstance(themed, dict):
            color_id = themed.get("ColorId")
            if not isinstance(color_id, int) or color_id not in expr.VERIFIED_THEME_COLOR_IDS:
                return f"{prop} uses a theme color that does not exist"
            return None
        if _is_literal_node(color):
            value = expr.decode({"solid": node["solid"]})
            if not (isinstance(value, dict) and isinstance(value.get("hex"), str)):
                return f"{prop} is not a color"
        return None
    if not _is_literal_node(node):
        return None  # a measure or another expression: not a literal to type
    value = expr.decode(node)
    suffix = expr.number_suffix(node)
    subtype = spec.get("subtype")
    if kind == "formatting":
        numeric = (
            subtype in _NUMERIC_SUBTYPES
            if subtype is not None
            else prop.casefold().endswith("size")
        )
        kind = "numeric" if numeric else "text"
    if kind == "bool":
        return None if isinstance(value, bool) else f"{prop} is not true or false"
    if kind in ("numeric", "integer"):
        # D and L are interchangeable when reading: Desktop writes 6L where the catalog says
        # numeric.
        return None if suffix in ("D", "L") else f"{prop} is not a number"
    if kind == "text":
        return None if isinstance(value, str) else f"{prop} is not text"
    if kind == "enum":
        ok = isinstance(value, str) or (suffix in ("D", "L") and not isinstance(value, bool))
        return None if ok else f"{prop} is not one of its choices"
    return None


def _entries(body: dict[str, Any]) -> dict[tuple[str, str, str, str], Any]:
    """(where, object, selector, property) -> the property's expression, for every entry."""
    found: dict[tuple[str, str, str, str], Any] = {}
    for where in ("objects", "visualContainerObjects"):
        objects = body.get(where)
        for object_name, entries in objects.items() if isinstance(objects, dict) else []:
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("properties"), dict):
                    continue
                selector = json.dumps(entry.get("selector"), sort_keys=True)
                for prop, node in entry["properties"].items():
                    found[(where, object_name, selector, prop)] = node
    return found


def _original_visual(document: PbirDocument, part: str) -> dict[str, Any]:
    try:
        loaded = decode_json(document.package.read(part))
    except (KeyError, PackageError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _check_literals(run: _Run, page: PageDoc, visual_id: str, visual: dict[str, Any]) -> None:
    part = _visual_part(page, visual_id)
    body = visual.get("visual")
    if not isinstance(body, dict):
        return
    visual_type = str(body.get("visualType", ""))
    before = _entries(_original_visual(run.document, part).get("visual") or {})
    for key, node in _entries(body).items():
        if key in before and before[key] == node:
            continue  # Desktop's own spelling, or an entry this batch did not touch
        where, object_name, _, prop = key
        found = expr.property_spec(visual_type, object_name, prop)
        pointer = _pointer("visual", where, object_name, prop)
        if found is None:
            run.blocking(
                "property_unknown",
                part,
                pointer,
                f"Visual {visual_id} has a {object_name}.{prop} setting that a {visual_type} "
                "visual does not have",
            )
            continue
        problem = _literal_problem(found[1], prop, node)
        if problem:
            run.blocking(
                "literal_type_mismatch",
                part,
                pointer,
                f"Visual {visual_id}: {problem}",
            )


# --- bookmarks ------------------------------------------------------------------------------


def _check_bookmarks(run: _Run) -> None:
    document = run.document
    pages = document.pages
    for key, bookmark in document.bookmarks().items():
        part = f"{ROOT}bookmarks/{key}.bookmark.json"
        label = f"The bookmark {bookmark.get('displayName') or key!r}"
        state = bookmark.get("explorationState")
        if isinstance(state, dict):
            active = state.get("activeSection")
            if isinstance(active, str) and active not in pages:
                run.blocking(
                    "bookmark_page_missing",
                    part,
                    _pointer("explorationState", "activeSection"),
                    f"{label} opens a page that does not exist",
                )
            sections = state.get("sections")
            for page_name, section in sections.items() if isinstance(sections, dict) else []:
                if page_name not in pages:
                    run.blocking(
                        "bookmark_page_missing",
                        part,
                        _pointer("explorationState", "sections", page_name),
                        f"{label} remembers a page that does not exist",
                    )
                    continue
                for member_key in ("visualContainers", "visualContainerGroups"):
                    members = section.get(member_key) if isinstance(section, dict) else None
                    for visual in members if isinstance(members, dict) else []:
                        if visual not in pages[page_name].visuals:
                            run.blocking(
                                "bookmark_visual_missing",
                                part,
                                _pointer(
                                    "explorationState", "sections", page_name, member_key, visual
                                ),
                                f"{label} remembers a visual that is not on page "
                                f"{pages[page_name].display_name}",
                            )
        options = bookmark.get("options")
        targets = options.get("targetVisualNames") if isinstance(options, dict) else None
        every = document.visual_names()
        for index, name in enumerate(targets if isinstance(targets, list) else []):
            if isinstance(name, str) and name not in every:
                run.blocking(
                    "bookmark_visual_missing",
                    part,
                    _pointer("options", "targetVisualNames", index),
                    f"{label} applies to a visual that does not exist",
                )


# Codes ``report_refs.navigation_findings`` shares with the checks above: reported once, above.
_NAVIGATION_CODES_CHECKED_HERE = frozenset(
    {
        "bookmark_page_missing",
        "bookmark_visual_missing",
        "link_page_missing",
        "interaction_visual_missing",
        "bound_filter_missing",
    }
)


def _check_navigation(run: _Run) -> None:
    """Navigation references the checks above do not cover (``report_refs.navigation_findings``,
    which the navigation family runs after each of its edits too): the bookmark list against
    the bookmark files, groups nested in a bookmark's group tree, a button's bookmark, a
    drillthrough or tooltip link to a page of the wrong type, a navigator's bookmark group,
    deselection bookmark and per-page settings, and sync groups that mix kinds of slicers. A
    dangling one blocks the save like any other broken reference."""
    for found in navigation_findings(run.document):
        if found.code not in _NAVIGATION_CODES_CHECKED_HERE:
            run.blocking(found.code, found.part, found.pointer, found.message)


# --- advisory checks ------------------------------------------------------------------------

_FRAME_TYPES = frozenset({"shape", "basicShape", "image", "textbox", "actionButton"})
# The visual types Studio's canvas draws: the keys of ``live_report.KINDS`` and of
# ``live_report.NAVIGATION_KINDS`` (buttons and navigators), copied because importing that
# module pulls in the whole report engine (a test keeps them equal).
DRAWN_TYPES = frozenset(
    {
        *("card", "cardVisual", "multiRowCard", "columnChart", "clusteredColumnChart"),
        *("stackedColumnChart", "barChart", "clusteredBarChart", "stackedBarChart"),
        *("lineChart", "areaChart", "stackedAreaChart", "pieChart", "donutChart"),
        *("tableEx", "pivotTable", "slicer", "advancedSlicerVisual", "textbox", "shape"),
        *("basicShape", "image", "visualGroup", "group"),
        *("actionButton", "pageNavigator", "bookmarkNavigator"),
    }
)


def _tab_order(visual: dict[str, Any]) -> float | None:
    position = visual.get("position")
    return _number(position.get("tabOrder")) if isinstance(position, dict) else None


def _check_advisory(run: _Run, page: PageDoc) -> None:
    boxes: list[tuple[str, tuple[float, float, float, float]]] = []
    tab_orders: dict[float, str] = {}
    any_tab_order = any(_tab_order(v) is not None for v in page.visuals.values())
    for visual_id, visual in page.visuals.items():
        part = _visual_part(page, visual_id)
        body = visual.get("visual")
        visual_type = str(body.get("visualType", "")) if isinstance(body, dict) else ""
        rect = _page_rect(page, visual_id)
        if rect is not None and (rect[2] < _MIN_SIZE or rect[3] < _MIN_SIZE):
            run.advisory(
                "size_below_minimum",
                part,
                _pointer("position"),
                f"Visual {visual_id} is smaller than {int(_MIN_SIZE)} pixels on a side",
            )
        if visual_type and visual_type not in DRAWN_TYPES:
            run.advisory(
                "powerbi_only_visual",
                part,
                _pointer("visual", "visualType"),
                f"Studio's canvas cannot draw visual {visual_id} ({visual_type}); it will say "
                "Needs Power BI there, and Power BI itself shows it",
            )
        if rect is not None and isinstance(body, dict) and visual_type not in _FRAME_TYPES:
            boxes.append((visual_id, rect))
        order = _tab_order(visual)
        if order is None and any_tab_order and "visual" in visual:
            run.advisory(
                "tab_order_gap",
                part,
                _pointer("position", "tabOrder"),
                f"Visual {visual_id} has no tab order, so keyboard users reach it unpredictably",
            )
        elif order is not None and "visualGroup" not in visual:
            if order in tab_orders:
                run.advisory(
                    "tab_order_duplicate",
                    part,
                    _pointer("position", "tabOrder"),
                    f"Visual {visual_id} shares its tab order with {tab_orders[order]}",
                )
            else:
                tab_orders[order] = visual_id
    if len(boxes) <= _MAX_OVERLAP_VISUALS:
        _check_overlap(run, page, boxes)


def _check_overlap(
    run: _Run, page: PageDoc, boxes: list[tuple[str, tuple[float, float, float, float]]]
) -> None:
    for i, (first, a) in enumerate(boxes):
        for second, b in boxes[i + 1 :]:
            width = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
            height = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
            if width <= 0 or height <= 0:
                continue
            if width * height >= _OVERLAP_SHARE * min(a[2] * a[3], b[2] * b[3]):
                run.advisory(
                    "visuals_overlap",
                    _visual_part(page, second),
                    _pointer("position"),
                    f"Visual {second} overlaps visual {first}",
                )


# Public names of the layout pieces the page review (report_review) holds a page to, so a
# review and the validation never disagree on what overlaps, what is too small or cut off.
EDGE_TOLERANCE = _EDGE_TOLERANCE
MIN_SIZE = _MIN_SIZE
FRAME_TYPES = _FRAME_TYPES
MAX_OVERLAP_VISUALS = _MAX_OVERLAP_VISUALS
page_rect = _page_rect
check_overlap = _check_overlap
ValidationRun = _Run


# --- entry points ---------------------------------------------------------------------------


def _duplicate_visual_names(run: _Run) -> None:
    seen: dict[str, str] = {}
    for page in run.document.pages.values():
        for visual_id in page.visuals:
            if visual_id in seen:
                run.blocking(
                    "visual_name_duplicate",
                    _visual_part(page, visual_id),
                    _pointer("name"),
                    "Two visuals share a name",
                )
            else:
                seen[visual_id] = page.name


def _all_findings(
    document: PbirDocument, model: ModelInfo | None, lineage: Mapping[str, str] | None = None
) -> list[Finding]:
    run = _Run(document, model, lineage)
    _duplicate_visual_names(run)
    changed = _changed_parts(document)
    for page in document.pages.values():
        for visual_id, visual in page.visuals.items():
            _check_visual_structure(run, page, visual_id, visual)
            _check_visual_position(run, page, visual_id, visual, changed)
            _check_selectors(run, page, visual_id, visual)
            _check_links(run, page, visual_id, visual)
            part = _visual_part(page, visual_id)
            _check_fields(run, part, f"visual {visual_id}", visual)
            if part in changed:
                _check_literals(run, page, visual_id, visual)
        _check_groups(run, page)
        _check_fields(run, _page_part(page), f"page {page.display_name}", page.data)
        _check_advisory(run, page)
    _check_fields(run, ROOT + "report.json", "the report", document.report)
    _check_report_and_pages(run)
    _check_filter_names(run)
    _check_filter_schemas(run)
    _check_bookmarks(run)
    _check_navigation(run)
    return run.findings


def _translate(key: FindingKey, roots: Mapping[str, str]) -> FindingKey:
    """The key a copy's finding had in its source, so a copy inherits the source's baseline.
    ``roots`` maps each copy to the original at the end of its chain of copies."""
    code, part, pointer = key
    for new, source in roots.items():
        part = part.replace(f"pages/{new}/", f"pages/{source}/")
        part = part.replace(f"visuals/{new}/", f"visuals/{source}/")
    return code, part, pointer


def _keys(finding: Finding) -> tuple[FindingKey, FindingKey]:
    """The stable key and the position-based one. A baseline holds both: the stable key
    survives entries being removed before the defect, the position-based one survives a copy
    (whose entries get new names but keep their positions)."""
    return finding.key, (finding.code, _relative(finding.part), finding.pointer)


def validate_findings(
    document: PbirDocument,
    *,
    model: ModelInfo | None = None,
    baseline: Baseline | None = None,
    lineage: Mapping[str, str] | None = None,
) -> list[Finding]:
    """Everything wrong or worth a look in the document, minus what ``baseline`` already had.

    ``baseline`` is ``capture_baseline`` of the document before the edit (with the same
    ``model``); ``lineage`` maps the names of visuals and pages the edit copied to their sources.
    ``model`` enables the checks that need the data model (fields must exist); without it, or
    with an empty one, those are skipped."""
    findings = _all_findings(document, model, lineage)
    if not baseline:
        return findings
    known = set(baseline)
    roots = _roots(lineage or {})
    return [
        f
        for f in findings
        if not any(k in known or _translate(k, roots) in known for k in _keys(f))
    ]


def capture_baseline(document: PbirDocument, model: ModelInfo | None = None) -> set[FindingKey]:
    """The keys of every finding the document has now: call it on the head before applying a
    batch, then pass the result as ``baseline`` after."""
    return {key for f in _all_findings(document, model) for key in _keys(f)}


def validate_document(
    document: PbirDocument,
    model: ModelInfo | None = None,
    *,
    baseline: Baseline | None = None,
    lineage: Mapping[str, str] | None = None,
) -> list[str]:
    """Blocking problems in plain English; an empty list means valid.

    Without ``baseline`` every blocking finding counts, so a source report that already has a
    problem is never valid; pass ``capture_baseline`` of the head to hold an edit only to the
    problems it introduced."""
    found = validate_findings(document, model=model, baseline=baseline, lineage=lineage)
    return [f.message for f in found if f.severity == "blocking"]
