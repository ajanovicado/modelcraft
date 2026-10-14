"""Compare two PBIX packages by meaning, not by bytes.

A Desktop save rewrites the whole file: key order can change, doubles are written with 17
digits (966.14173228346453 where Python writes 966.1417322834645), the DataModel bytes
change and unreferenced resources are deleted. A byte or hash comparison would therefore
report a change after every save. Two questions need a semantic answer instead:

- ``semantic_diff``: did anything change? Used to skip no-op revisions, to tell a user's
  Desktop work from Desktop's own rewriting, and to say what a revision changed.
- ``survival``: after Desktop opened a file Studio wrote and saved it, is every value
  Studio wrote still there? An edit Desktop silently dropped must fail the batch.

Report JSON (``Report/**/*.json``) is compared as parsed JSON. Every other part is compared
by CRC-32 and size from the zip's central directory, so a DataModel of any size is never
decompressed, and a DataModel whose bytes differ does not make the diff non-empty (Desktop
rewrites it on every save; ``compare_model=True`` compares measures, formats and
relationships instead). The normalizations that are NOT differences: SecurityBindings and its
content-type entry, content-type element order, a ``$schema`` whose version changed but
whose schema name did not, a JSON part re-encoded (BOM, UTF-16), key order, whitespace and
float spelling.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final
from xml.etree import ElementTree as ET

from .pbix_package import PackageError, PbixPackage, decode_json

_CONTENT_TYPES = "[Content_Types].xml"
_IGNORED_PARTS = frozenset({"SecurityBindings"})
_SCHEMA_VERSION = re.compile(r"/\d+\.\d+\.\d+/schema\.json$")
# A numeric literal as Desktop writes it inside a property expression: 16D, 45L, 1.5M.
_NUMBER_LITERAL = re.compile(r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)([DLM])")
_PAGES = "Report/definition/pages/"
_REGISTERED = "Report/StaticResources/RegisteredResources/"
_SHARED = "Report/StaticResources/SharedResources/"
_RELATIVE_TOLERANCE = 1e-12


class _Missing:
    """The value of a key or list item that is absent (JSON null is a different thing)."""

    def __repr__(self) -> str:
        return "<missing>"

    def __bool__(self) -> bool:
        return False


MISSING: Final = _Missing()


@dataclass(frozen=True)
class Change:
    """One difference inside a JSON part. ``path`` is a JSON pointer ("" for the whole part).

    ``before`` or ``after`` is ``MISSING`` when the value was added or removed.
    """

    part: str
    path: str
    before: Any
    after: Any


@dataclass(frozen=True)
class SemanticDiff:
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[Change, ...]
    binary_changed: tuple[str, ...]
    # part name -> what a person calls it ("Visual 'Sales by region'"), for ``summary``.
    labels: tuple[tuple[str, str], ...] = ()
    # What differs in the model's measures, formats and relationships. Only filled when
    # ``semantic_diff(..., compare_model=True)`` read both models; see ``empty``.
    model_changes: tuple[str, ...] = ()

    @property
    def model_rewritten(self) -> bool:
        """True when the DataModel bytes differ. Desktop does that on every save, so this
        alone is not a change to the report; ``model_changes`` says whether it is one."""
        return "DataModel" in self.binary_changed

    @property
    def empty(self) -> bool:
        """True when the two files hold the same report.

        The DataModel is left out on purpose: Desktop rewrites its bytes on every save, so
        counting them would make a file Desktop saved without any change never "the same",
        and adoption (report_revisions.semantic_equal) would record a revision per save.
        Pass ``compare_model=True`` to ``semantic_diff`` when a change to measures, formats
        or relationships must also count; it is not the default because reading a model
        means decompressing it.
        """
        others = [part for part in self.binary_changed if part != "DataModel"]
        return not (self.added or self.removed or self.changed or others or self.model_changes)

    def summary(self, limit: int = 20) -> list[str]:
        """Plain-English lines, at most ``limit``: "Visual 'Sales by region': title changed"."""
        names = dict(self.labels)
        lines: list[str] = []
        phrases: dict[str, list[str]] = {}
        for change in self.changed:
            bucket = phrases.setdefault(change.part, [])
            for phrase in _change_phrases(change):
                if phrase not in bucket:
                    bucket.append(phrase)
        for part, bucket in phrases.items():
            lines.append(f"{_name(part, names)}: {', '.join(bucket)} changed")
        lines += _membership_lines(self.added, names, "added")
        lines += _membership_lines(self.removed, names, "removed")
        for part in self.binary_changed:
            if part != "DataModel":
                lines.append(f"{_name(part, names)} changed")
        lines += self.model_changes
        limit = max(limit, 1)
        if len(lines) > limit:
            hidden = len(lines) - (limit - 1)
            lines = [*lines[: limit - 1], f"...and {hidden} more changes"]
        return lines


# --- comparing values -------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _schema_name(url: str) -> str | None:
    return _SCHEMA_VERSION.sub("", url) if _SCHEMA_VERSION.search(url) else None


def _same_scalar(a: Any, b: Any, *, lenient: bool) -> bool:
    """Equality that ignores float spelling and ``$schema``-style version bumps.

    ``lenient`` also treats the D (double) and L (long) suffixes of an equal number as the
    same literal: Desktop reads them interchangeably, so 6D saved back as 6L is not a
    lost edit.
    """
    if _is_number(a) and _is_number(b):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        return math.isclose(a, b, rel_tol=_RELATIVE_TOLERANCE)
    if isinstance(a, str) and isinstance(b, str):
        if a == b:
            return True
        left, right = _NUMBER_LITERAL.fullmatch(a), _NUMBER_LITERAL.fullmatch(b)
        if left and right:
            kinds = {left.group(2), right.group(2)}
            if left.group(2) != right.group(2) and not (lenient and kinds == {"D", "L"}):
                return False
            if left.group(2) == right.group(2) == "L" and "." not in a + b:
                return int(left.group(1)) == int(right.group(1))
            return math.isclose(
                float(left.group(1)), float(right.group(1)), rel_tol=_RELATIVE_TOLERANCE
            )
        return False
    if type(a) is not type(b):
        return False
    return bool(a == b)


def _same_schema(a: Any, b: Any) -> bool:
    if not (isinstance(a, str) and isinstance(b, str)):
        return False
    name = _schema_name(a)
    return a == b or (name is not None and name == _schema_name(b))


def _equal(a: Any, b: Any, *, lenient: bool = False) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_equal(a[k], b[k], lenient=lenient) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(
            _equal(x, y, lenient=lenient) for x, y in zip(a, b, strict=True)
        )
    if isinstance(a, dict | list) or isinstance(b, dict | list):
        return False
    return _same_scalar(a, b, lenient=lenient)


def _escape(segment: str) -> str:
    return segment.replace("~", "~0").replace("/", "~1")


def _pointer_segments(pointer: str) -> list[str]:
    if not pointer:
        return []
    return [s.replace("~1", "/").replace("~0", "~") for s in pointer.split("/")[1:]]


def _diff_value(part: str, a: Any, b: Any, pointer: str, out: list[Change]) -> None:
    if isinstance(a, dict) and isinstance(b, dict):
        for key in a:
            here = f"{pointer}/{_escape(key)}"
            if key not in b:
                out.append(Change(part, here, a[key], MISSING))
            elif key == "$schema" and _same_schema(a[key], b[key]):
                continue
            else:
                _diff_value(part, a[key], b[key], here, out)
        out.extend(Change(part, f"{pointer}/{_escape(k)}", MISSING, b[k]) for k in b if k not in a)
    elif isinstance(a, list) and isinstance(b, list):
        for index in range(max(len(a), len(b))):
            here = f"{pointer}/{index}"
            if index >= len(b):
                out.append(Change(part, here, a[index], MISSING))
            elif index >= len(a):
                out.append(Change(part, here, MISSING, b[index]))
            else:
                _diff_value(part, a[index], b[index], here, out)
    elif not _equal(a, b):
        out.append(Change(part, pointer, a, b))


# --- packages ---------------------------------------------------------------------------


def _open(package: Path | PbixPackage) -> PbixPackage:
    return package if isinstance(package, PbixPackage) else PbixPackage(Path(package))


def _is_report_json(name: str) -> bool:
    return name.startswith("Report/") and name.lower().endswith(".json")


def _load_json(package: PbixPackage, names: list[str]) -> dict[str, Any]:
    """Every listed part that parses as JSON; a part that does not is left out."""
    try:
        blobs = package.read_parts(names)
    except PackageError:
        blobs = {}
        for name in names:
            try:
                blobs[name] = package.read(name)
            except PackageError:
                continue
    parsed: dict[str, Any] = {}
    for name, data in blobs.items():
        try:
            parsed[name] = decode_json(data)
        except PackageError:
            continue
    return parsed


def _content_types(package: PbixPackage) -> dict[str, str]:
    """Content type by extension (".png") or part ("/report/x"), SecurityBindings excluded."""
    try:
        text = package.read(_CONTENT_TYPES).decode("utf-8-sig")
    except (KeyError, PackageError, UnicodeDecodeError):
        return {}
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        return {}
    try:
        root = ET.fromstring(text)  # noqa: S314 - bounded input, DTD/entities rejected above
    except ET.ParseError:
        return {}
    types: dict[str, str] = {}
    for element in root:
        kind = element.tag.rpartition("}")[2]
        if kind == "Default":
            types["." + element.get("Extension", "").casefold()] = element.get("ContentType", "")
        elif kind == "Override":
            part = element.get("PartName", "")
            if part.lstrip("/") not in _IGNORED_PARTS:
                types[part.casefold()] = element.get("ContentType", "")
    return types


def _model_snapshot(package: PbixPackage) -> dict[str, Any] | None:
    """Measures, format strings and relationships of a package's model, or None when they
    cannot be read (a model Studio replaced in memory, or a reader failure)."""
    if package.is_modified("DataModel"):
        return None
    try:
        from . import pbix_extract

        found = {
            "measures": pbix_extract.measures(package.source),
            "formats": pbix_extract.formats(package.source),
            "relationships": pbix_extract.read_relationships(package.source),
        }
    except Exception:  # noqa: BLE001 - any reader failure means "cannot compare", never a crash
        return None
    return found


def _keyed(rows: Any) -> dict[str, Any]:
    keyed: dict[str, Any] = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            key = f"{row.get('TableName', '')}[{row.get('Name', '')}]"
            keyed[key] = {k: v for k, v in row.items() if k not in ("TableName", "Name")}
    return keyed


def _model_differences(left: PbixPackage, right: PbixPackage) -> tuple[str, ...]:
    """Plain-English differences between two models' measures, formats and relationships.

    When either model cannot be read the answer is "changed": an unreadable model must not
    be taken for an unchanged one, because that would hide a real model edit.
    """
    one, two = _model_snapshot(left), _model_snapshot(right)
    if one is None or two is None:
        return ("The data model changed",)
    lines: list[str] = []
    old, new = _keyed(one["measures"]), _keyed(two["measures"])
    lines += [f"Measure '{n}' was removed" for n in old.keys() - new.keys()]
    lines += [f"Measure '{n}' was added" for n in new.keys() - old.keys()]
    lines += [f"Measure '{n}' changed" for n in old.keys() & new.keys() if old[n] != new[n]]
    old, new = _keyed(one["formats"]), _keyed(two["formats"])
    lines += [
        f"Format of measure '{n}' changed" for n in old.keys() & new.keys() if old[n] != new[n]
    ]
    if _canonical(one["relationships"]) != _canonical(two["relationships"]):
        lines.append("The relationships changed")
    return tuple(sorted(lines))


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def semantic_diff(
    a: Path | PbixPackage, b: Path | PbixPackage, *, compare_model: bool = False
) -> SemanticDiff:
    """What differs between ``a`` and ``b``, ignoring everything Desktop rewrites freely.

    The DataModel is compared by CRC only and does not make the diff non-empty (see
    ``SemanticDiff.empty``). ``compare_model=True`` also reads both models' measures, format
    strings and relationships (slow: it decompresses the models) and reports differences
    in ``model_changes``.
    """
    left, right = _open(a), _open(b)
    in_a = [n for n in left.names() if n not in _IGNORED_PARTS]
    in_b = [n for n in right.names() if n not in _IGNORED_PARTS]
    set_a, set_b = set(in_a), set(in_b)
    added = tuple(sorted(set_b - set_a))
    removed = tuple(sorted(set_a - set_b))
    common = [n for n in in_a if n in set_b and n != _CONTENT_TYPES]

    docs_a = _load_json(left, [n for n in in_a if _is_report_json(n)])
    docs_b = _load_json(right, [n for n in in_b if _is_report_json(n)])
    changed: list[Change] = []
    binary: list[str] = []
    for name in common:
        if name in docs_a and name in docs_b:
            _diff_value(name, docs_a[name], docs_b[name], "", changed)
        elif left.fingerprint(name) != right.fingerprint(name):
            binary.append(name)
    types_a, types_b = _content_types(left), _content_types(right)
    if any(types_a[k] != types_b[k] for k in types_a.keys() & types_b.keys()):
        binary.append(_CONTENT_TYPES)

    touched = sorted({*added, *removed, *binary, *(c.part for c in changed)})
    # Name a part as it was before the change ("Visual 'Sales by region': title changed"),
    # except a part that is new, which only exists after it.
    labels = tuple(
        (part, _safe_label(part, docs_a if part in set_a else docs_b)) for part in touched
    )
    model = _model_differences(left, right) if compare_model and "DataModel" in binary else ()
    return SemanticDiff(added, removed, tuple(changed), tuple(binary), labels, model)


# --- names people recognise ---------------------------------------------------------------


def _literal_text(node: Any) -> str | None:
    """The string inside a ``{"expr": {"Literal": {"Value": "'text'"}}}`` node, if it is one."""
    if not isinstance(node, dict):
        return None
    value = node.get("expr", node)
    inner = value.get("Literal") if isinstance(value, dict) else None
    literal = inner.get("Value") if isinstance(inner, dict) else None
    if isinstance(literal, str) and len(literal) >= 2 and literal[0] == literal[-1] == "'":
        return literal[1:-1].replace("''", "'")
    return None


def _entries_text(entries: Any) -> str | None:
    """The first non-blank ``text`` literal among an object's entries."""
    for entry in entries if isinstance(entries, list) else []:
        props = entry.get("properties", {}) if isinstance(entry, dict) else {}
        text = _literal_text(props.get("text")) if isinstance(props, dict) else None
        if text and text.strip():
            return text.strip()
    return None


def _visual_title(name: str, visual: dict[str, Any]) -> str:
    """The text a person would call the visual: its title, caption or first field."""
    body = visual.get("visual")
    if isinstance(body, dict):
        containers = body.get("visualContainerObjects", {})
        objects = body.get("objects", {})
        for group, key in ((containers, "title"), (objects, "header"), (objects, "label")):
            found = _entries_text(group.get(key)) if isinstance(group, dict) else None
            if found:
                return found
        query = body.get("query")
        states = query.get("queryState") if isinstance(query, dict) else None
        if isinstance(states, dict):
            for role in states.values():
                projections = role.get("projections") if isinstance(role, dict) else None
                for projection in projections if isinstance(projections, list) else []:
                    if not isinstance(projection, dict):
                        continue
                    label = projection.get("displayName") or projection.get("nativeQueryRef")
                    if isinstance(label, str) and label.strip():
                        return label.strip()
        kind = body.get("visualType")
        if isinstance(kind, str):
            return f"{kind} {name[:6]}"
    group_body = visual.get("visualGroup")
    if isinstance(group_body, dict) and isinstance(group_body.get("displayName"), str):
        return str(group_body["displayName"])
    return name[:8]


def _part_kind(part: str) -> str:
    if part == "Report/definition/report.json":
        return "report"
    if part == _PAGES + "pages.json":
        return "pages"
    if part.startswith(_PAGES):
        rest = part[len(_PAGES) :].split("/")
        if len(rest) == 2 and rest[1] == "page.json":
            return "page"
        if len(rest) == 4 and rest[1] == "visuals":
            return "mobile" if rest[3] == "mobile.json" else "visual"
    if part == "Report/definition/bookmarks/bookmarks.json":
        return "bookmarks"
    if part.startswith("Report/definition/bookmarks/") and part.endswith(".bookmark.json"):
        return "bookmark"
    if part == "Report/definition/version.json":
        return "version"
    if part == "Report/definition/reportExtensions.json":
        return "extensions"
    if part.startswith(("Report/StaticResources/", "Report/CustomVisuals/")):
        return "resource"
    return "other"


def _page_title(docs: dict[str, Any], page_name: str) -> str:
    page = docs.get(f"{_PAGES}{page_name}/page.json")
    if isinstance(page, dict) and isinstance(page.get("displayName"), str):
        return str(page["displayName"])
    return page_name


def _label(part: str, docs: dict[str, Any]) -> str:
    kind = _part_kind(part)
    document = docs.get(part)
    rest = part[len(_PAGES) :].split("/") if part.startswith(_PAGES) else []
    if kind == "report":
        return "The report"
    if kind == "pages":
        return "The page list"
    if kind == "page":
        return f"Page '{_page_title(docs, rest[0])}'"
    if kind in ("visual", "mobile"):
        visual = docs.get(f"{_PAGES}{rest[0]}/visuals/{rest[2]}/visual.json")
        title = _visual_title(rest[2], visual) if isinstance(visual, dict) else rest[2][:8]
        pages = sum(1 for n in docs if _part_kind(n) == "page")
        where = f" on page '{_page_title(docs, rest[0])}'" if pages > 1 else ""
        prefix = "Phone layout of visual" if kind == "mobile" else "Visual"
        return f"{prefix} '{title}'{where}"
    if kind == "bookmarks":
        return "The bookmark list"
    if kind == "bookmark":
        shown = document.get("displayName") if isinstance(document, dict) else None
        stem = PurePosixPath(part).name.removesuffix(".bookmark.json")
        return f"Bookmark '{shown if isinstance(shown, str) else stem}'"
    if kind == "version":
        return "The report version"
    if kind == "extensions":
        return "The report measures"
    base = PurePosixPath(part).name
    if kind == "resource" and part.lower().endswith(".json"):
        return f"Theme '{base}'"
    return "The data model" if part == "DataModel" else f"'{base}'"


def _safe_label(part: str, docs: dict[str, Any]) -> str:
    """``_label``, but naming a part can never block a comparison: a part with an unexpected
    shape is called by its file name."""
    try:
        return _label(part, docs)
    except (AttributeError, TypeError, KeyError, IndexError, ValueError):
        return f"'{PurePosixPath(part).name}'"


def _name(part: str, labels: dict[str, str]) -> str:
    return labels.get(part) or f"'{PurePosixPath(part).name}'"


def _membership_lines(parts: tuple[str, ...], names: dict[str, str], verb: str) -> list[str]:
    """One line per added or removed part; a page's visuals fold into the page's line."""
    pages = {p.split("/")[3] for p in parts if _part_kind(p) == "page"}
    visuals: dict[str, int] = {}
    for part in parts:
        if _part_kind(part) in ("visual", "mobile"):
            page = part.split("/")[3]
            if page in pages:
                visuals[page] = visuals.get(page, 0) + (_part_kind(part) == "visual")
    lines = []
    for part in parts:
        kind = _part_kind(part)
        if kind in ("visual", "mobile") and part.split("/")[3] in pages:
            continue
        line = f"{_name(part, names)} {verb}"
        if kind == "page":
            count = visuals.get(part.split("/")[3], 0)
            if count:
                line += f" (with {count} visual{'s' if count != 1 else ''})"
        lines.append(line)
    return lines


_CONTAINER_LABELS = {
    "title": "title",
    "subTitle": "subtitle",
    "background": "background",
    "border": "border",
    "dropShadow": "shadow",
    "visualHeader": "header icons",
    "visualHeaderTooltip": "header tooltip",
    "visualLink": "button action",
    "visualTooltip": "tooltip",
    "padding": "padding",
    "spacing": "spacing",
    "divider": "divider",
    "lockAspect": "aspect ratio lock",
    "general": "general settings",
    "stylePreset": "style preset",
}
_OBJECT_LABELS = {
    "dataPoint": "data colors",
    "labels": "data labels",
    "legend": "legend",
    "categoryAxis": "category axis",
    "valueAxis": "value axis",
    "values": "values",
    "header": "header",
    "label": "caption",
    "data": "data settings",
    "fillCustom": "fill",
    "outline": "outline",
    "general": "general settings",
    "columnHeaders": "column headers",
    "grid": "grid",
}
_PAGE_LABELS = {
    "displayName": "name",
    "width": "size",
    "height": "size",
    "displayOption": "size",
    "visibility": "visibility",
    "filterConfig": "filters",
    "pageBinding": "drillthrough or tooltip settings",
    "visualInteractions": "visual interactions",
    "type": "page type",
    "name": "identifier",
}
_REPORT_LABELS = {
    "themeCollection": "theme",
    "resourcePackages": "resources",
    "objects": "report settings",
    "settings": "report options",
    "filterConfig": "report filters",
    "annotations": "annotations",
    "slowDataSourceSettings": "report options",
}


def _words(key: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", key).lower()


def _change_phrases(change: Change) -> list[str]:
    """What a change is called. A whole formatting block that was added or removed is named
    by what is inside it ("title", "background"), not just "container formatting"."""
    segments = _pointer_segments(change.path)
    if (change.before is MISSING) != (change.after is MISSING):
        return _block_phrases(
            change.part, segments, change.after if change.before is MISSING else change.before
        )
    return [_phrase(change.part, segments)]


def _block_phrases(part: str, segments: list[str], whole: Any) -> list[str]:
    kind = _part_kind(part)
    block = (
        kind == "visual"
        and len(segments) == 2
        and segments[0] == "visual"
        and segments[1] in ("visualContainerObjects", "objects")
    ) or (kind == "page" and segments == ["objects"])
    if block and isinstance(whole, dict) and whole:
        return [_phrase(part, [*segments, str(key)]) for key in whole]
    return [_phrase(part, segments)]


def _phrase(part: str, segments: list[str]) -> str:
    """A short description of what a changed JSON path is, in Power BI's own words."""
    kind = _part_kind(part)
    first = segments[0] if segments else ""
    if kind in ("visual", "mobile"):
        if kind == "mobile":
            return "phone layout"
        if first == "position":
            return "position or size"
        if first == "isHidden":
            return "visibility"
        if first == "filterConfig":
            return "filters"
        if first == "parentGroupName":
            return "group membership"
        if first == "visualGroup":
            return "group settings"
        if first == "visual" and len(segments) > 1:
            second = segments[1]
            if second == "visualType":
                return "visual type"
            if second == "query":
                third = segments[2] if len(segments) > 2 else ""
                return "sort order" if third == "sortDefinition" else "fields"
            if second in ("visualContainerObjects", "objects"):
                if len(segments) == 2:
                    return "formatting" if second == "objects" else "container formatting"
                table = _CONTAINER_LABELS if second == "visualContainerObjects" else _OBJECT_LABELS
                name = table.get(segments[2], _words(segments[2]))
                return name if second == "visualContainerObjects" else f"{name} formatting"
            if second == "drillFilterOtherVisuals":
                return "cross-filtering"
            return _words(second)
        return _words(first) if first else "settings"
    if kind == "page":
        if first == "objects" and len(segments) > 1:
            name = _words(segments[1])
            return name if name == "background" else f"{name} formatting"
        return _PAGE_LABELS.get(first, _words(first) if first else "settings")
    if kind == "report":
        return _REPORT_LABELS.get(first, _words(first) if first else "settings")
    if kind == "pages":
        return {"pageOrder": "page order", "activePageName": "active page"}.get(
            first, _words(first) if first else "settings"
        )
    if kind == "bookmark":
        return {"displayName": "name", "explorationState": "saved view", "options": "options"}.get(
            first, _words(first) if first else "settings"
        )
    if kind == "resource":
        return "theme"
    return _words(first) if first else "contents"


# --- survival ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Lost:
    segments: tuple[str, ...]
    reason: str  # "dropped" | "changed" | "order"
    wrote: Any
    found: Any


def _lost(w: Any, s: Any, base: Any, segments: tuple[str, ...], out: list[_Lost]) -> None:
    """Record every value in ``w`` that ``s`` lacks. ``base`` (the pre-edit value, or MISSING)
    exempts anything the edit did not touch, so Desktop's own rewriting is not blamed on it."""
    if base is not MISSING and _equal(base, w, lenient=True):
        return
    if isinstance(w, dict) and isinstance(s, dict):
        for key, value in w.items():
            here = (*segments, key)
            previous = base.get(key, MISSING) if isinstance(base, dict) else MISSING
            if key not in s:
                # An empty object or list has nothing to preserve; Desktop omits them.
                if value in ({}, []) or (
                    previous is not MISSING and _equal(previous, value, lenient=True)
                ):
                    continue
                out.append(_Lost(here, "dropped", value, MISSING))
            elif key == "$schema" and _same_schema(value, s[key]):
                continue
            else:
                _lost(value, s[key], previous, here, out)
    elif isinstance(w, list) and isinstance(s, list):
        _lost_list(w, s, base, segments, out)
    elif not _equal(w, s, lenient=True):
        out.append(_Lost(segments, "changed", w, s))


def _survives(w: Any, s: Any, base: Any) -> bool:
    found: list[_Lost] = []
    _lost(w, s, base, (), found)
    return not found


def _lost_list(
    w: list[Any], s: list[Any], base: Any, segments: tuple[str, ...], out: list[_Lost]
) -> None:
    """Every element of ``w`` must appear in ``s``, in any order for lists of objects (Desktop
    reorders some of them) but in order for lists of plain values (page order, palettes)."""
    scalars = all(not isinstance(x, dict | list) for x in w)
    used: set[int] = set()
    in_order = True
    cursor = 0
    unmatched: list[tuple[int, Any]] = []
    for index, item in enumerate(w):
        if isinstance(base, list) and any(_equal(b, item, lenient=True) for b in base):
            continue  # an element the edit did not touch
        reference = base[index] if isinstance(base, list) and index < len(base) else MISSING
        match = next(
            (
                j
                for j in range(cursor, len(s))
                if j not in used and _survives(item, s[j], reference)
            ),
            None,
        )
        if match is None:
            in_order = False
            match = next(
                (j for j in range(len(s)) if j not in used and _survives(item, s[j], reference)),
                None,
            )
        if match is None:
            unmatched.append((index, reference))
        else:
            used.add(match)
            cursor = match + 1 if in_order else cursor
    for index, reference in unmatched:
        here = (*segments, str(index))
        if index < len(s) and index not in used:
            _lost(w[index], s[index], reference, here, out)
        else:
            out.append(_Lost(here, "dropped", w[index], MISSING))
    if scalars and not unmatched and not in_order:
        out.append(_Lost(segments, "order", w, s))


def _shown(value: Any) -> str:
    """A value as a person reads it: 'text' quoted once, 13D and true bare."""
    if isinstance(value, str):
        is_literal = (
            (len(value) >= 2 and value[0] == value[-1] == "'")
            or _NUMBER_LITERAL.fullmatch(value) is not None
            or value in ("true", "false")
        )
        text = value if is_literal else f"'{value}'"
    elif value is MISSING:
        text = "nothing"
    else:
        text = repr(value)
    return text if len(text) <= 48 else text[:45] + "..."


def _registered_items(document: Any) -> dict[str, tuple[str, str, str]]:
    """RegisteredResources items of a report.json: name -> (item type, part, name)."""
    found: dict[str, tuple[str, str, str]] = {}
    packages = document.get("resourcePackages", []) if isinstance(document, dict) else []
    for package in packages:
        if not isinstance(package, dict) or package.get("type") != "RegisteredResources":
            continue
        folder = f"Report/StaticResources/{package.get('name', 'RegisteredResources')}/"
        for item in package.get("items", []):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                path = str(item.get("path", item["name"]))
                found[item["name"]] = (str(item.get("type", "")), folder + path, item["name"])
    return found


_ITEM_WORDS = {"Image": "image", "CustomTheme": "theme", "BaseTheme": "theme"}


def _kept_as(item: tuple[str, str, str]) -> str:
    kind = _ITEM_WORDS.get(item[0], item[0] or "a resource")
    article = "an" if kind[:1] in "aeiou" else "a"
    return f"{article} {kind} at '{PurePosixPath(item[1]).name}'"


def survival(
    written: Path | PbixPackage,
    saved: Path | PbixPackage,
    baseline: Path | PbixPackage | None = None,
) -> list[str]:
    """What Studio wrote that Power BI Desktop did not keep when it saved the file.

    ``written`` is the file Studio produced, ``saved`` is what Desktop saved after opening
    it. Returns plain-English lines: a value in ``written``'s report JSON that is absent or
    different in ``saved``, and every RegisteredResources item Desktop dropped. Additions
    in ``saved`` (properties Desktop adds), key order, float spelling, ``$schema`` version
    bumps, and a SecurityBindings that Desktop wrote are not reported.

    Pass ``baseline`` (the file Studio started from) to blame only the edit: anything
    identical in ``baseline`` and ``written`` is skipped, so Desktop rewriting or
    garbage-collecting things Studio never touched (an unused image, a newer base theme)
    is not reported as a lost edit.
    """
    mine, theirs = _open(written), _open(saved)
    before = _open(baseline) if baseline is not None else None
    in_w = [n for n in mine.names() if _is_report_json(n)]
    docs_w = _load_json(mine, in_w)
    docs_s = _load_json(theirs, [n for n in theirs.names() if _is_report_json(n)])
    docs_b = _load_json(before, [n for n in before.names() if _is_report_json(n)]) if before else {}
    labels_source = {**docs_s, **docs_w}
    lines: list[str] = []

    dropped_items: set[str] = set()
    report = "Report/definition/report.json"
    if report in docs_w:
        wrote_items = _registered_items(docs_w[report])
        kept_items = _registered_items(docs_s.get(report, {}))
        base_items = _registered_items(docs_b.get(report, {})) if report in docs_b else {}
        for name, (item_type, part, _) in wrote_items.items():
            word = _ITEM_WORDS.get(item_type, "resource")
            if name in kept_items:
                # Kept under its name is not enough: an item Desktop re-typed or re-pathed
                # no longer points at what Studio wrote (the theme pointer can look intact).
                if (
                    kept_items[name][:2] != (item_type, part)
                    and base_items.get(name) != (wrote_items[name])
                ):
                    lines.append(
                        f"Power BI Desktop changed the {word} '{name}' the edit registered "
                        f"(it is now {_kept_as(kept_items[name])})"
                    )
                continue
            if base_items.get(name) == wrote_items[name] and _same_part(mine, before, part):
                continue  # the edit never touched it; Desktop garbage-collected it
            dropped_items.add(part)
            lines.append(f"Power BI Desktop dropped the {word} '{name}' the edit registered")

    seen: set[tuple[str, str]] = set()
    for part in in_w:
        if part.startswith(_SHARED):
            continue  # base themes are rewritten on every save
        wrote = docs_w.get(part)
        if wrote is None:
            continue
        previous = docs_b.get(part, MISSING)
        if part not in docs_s:
            if previous is not MISSING and _equal(previous, wrote, lenient=True):
                continue
            if part in dropped_items:
                continue
            label = _safe_label(part, labels_source)
            lines.append(f"{label} was removed by Power BI Desktop")
            continue
        found: list[_Lost] = []
        target: Any = docs_s[part]
        if part == report:
            wrote = {k: v for k, v in wrote.items() if k != "resourcePackages"}
            target = {k: v for k, v in target.items() if k != "resourcePackages"}
            if isinstance(previous, dict):
                previous = {k: v for k, v in previous.items() if k != "resourcePackages"}
        _lost(wrote, target, previous, (), found)
        for lost in found:
            if lost.reason == "dropped":
                phrases = _block_phrases(part, list(lost.segments), lost.wrote)
            else:
                phrases = [_phrase(part, list(lost.segments))]
            for phrase in phrases:
                if (part, phrase) in seen:
                    continue
                seen.add((part, phrase))
                label = _safe_label(part, labels_source)
                lines.append(f"{label}: {_lost_text(lost, phrase)}")

    for part in mine.names():
        if not part.startswith(_REGISTERED) or _is_report_json(part) or part in dropped_items:
            continue
        if theirs.has(part) or _same_part(mine, before, part):
            continue
        lines.append(f"Power BI Desktop dropped the file '{PurePosixPath(part).name}'")
    return lines


def _same_part(mine: PbixPackage, before: PbixPackage | None, part: str) -> bool:
    """True when the baseline has this part with identical bytes: the edit did not touch it."""
    if before is None or not before.has(part) or not mine.has(part):
        return False
    return mine.fingerprint(part) == before.fingerprint(part)


def _lost_text(lost: _Lost, phrase: str) -> str:
    if lost.reason == "order":
        return f"the order of {phrase} was not kept"
    # "filters were not kept", "title was not kept".
    verb = "were" if phrase.endswith("s") and not phrase.endswith("ss") else "was"
    if lost.reason == "dropped":
        return f"{phrase} {verb} not kept"
    return (
        f"{phrase} {verb} not kept "
        f"(Studio wrote {_shown(lost.wrote)}, Desktop saved {_shown(lost.found)})"
    )
