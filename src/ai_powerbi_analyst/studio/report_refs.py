"""Every place a report refers to a measure, a visual, a page or a filter by name.

An edit that renames or deletes something is only safe when every reference is found, and
a report refers to the same thing in many shapes:

- fields in projections, formatting rules (font colors, SVG image measures), sorts, and
  visual, page and report filters, where a filter's ``Where`` names tables through a
  ``From`` alias (``SourceRef.Source``) instead of ``SourceRef.Entity``;
- bookmarks (their own filters and per-page visual states) and DAX query files;
- other measures, calculated columns and field-parameter tables whose DAX uses a measure,
  so a measure no visual shows can still be needed by one that does;
- visuals by ``name`` (interactions, bookmarks, groups), pages by folder name (buttons,
  tooltips, drillthrough, bookmarks) and filters by ``name`` (bookmarks, bound filters);
- bookmarks by id (buttons, a bookmark navigator's deselection bookmark) and bookmark groups
  by id (a bookmark navigator's group). ``navigation_findings`` lists every such reference
  that dangles or points at the wrong kind of page.

The module reads the report only through the ``PbirDocument`` and its package, never the
data model, and imports nothing from ``report_edits`` so the edit layer can call it.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Collection, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from .pbir_document import (
    BOOKMARKS,
    BOOKMARKS_META,
    EXTENSIONS,
    ROOT,
    DocumentError,
    PageDoc,
    PbirDocument,
)
from .pbix_package import PackageError

USE_KINDS = (
    "projection",
    "object_expression",
    "selector",
    "sort",
    "filter_field",
    "filter_where",
    "page_filter",
    "report_filter",
    "bookmark",
    "dax_query",
    "measure_dax",
    "calculated_column",
    "calculated_table",
    "field_parameter",
    "visual_interaction",
    "group_member",
    "visual_link",
    "tooltip",
    "drillthrough",
    "page_binding",
    "bound_filter",
    "bookmark_action",  # a button's (or shape's) action opens the bookmark
    "bookmark_navigator",  # a bookmark navigator lists the group, or opens it on deselection
)

# Uses that live in the data model (DAX of measures, columns and tables) carry this part.
MODEL_PART = "DataModel"

_PAGE_PART = re.compile(re.escape(ROOT) + r"pages/([^/]+)/page\.json")
_VISUAL_PART = re.compile(re.escape(ROOT) + r"pages/([^/]+)/visuals/([^/]+)/visual\.json")
_FILTER_KINDS = ("filter_field", "filter_where", "page_filter", "report_filter")
_VISUAL_KINDS = ("projection", "object_expression", "selector", "sort", *_FILTER_KINDS[:2])
_DAX_KINDS = ("measure_dax", "calculated_column", "calculated_table", "field_parameter")


@dataclass(frozen=True)
class Use:
    kind: str  # one of USE_KINDS
    part: str  # part name, e.g. "Report/definition/pages/<p>/visuals/<v>/visual.json"
    where: str  # JSON pointer inside the part ("/line/<n>" for a DAX query file)
    label: str  # plain-English description for messages, e.g. "table 'Agent ID' (filter)"


@dataclass
class RenameResult:
    counts: dict[str, int] = field(default_factory=dict)  # rewrites per USE_KIND
    parts: set[str] = field(default_factory=set)  # parts changed
    # Uses the report layer cannot rewrite: DAX in the data model and DAX query files still
    # name the old measure. Only filled when the caller passes its ReferenceIndex.
    unrewritten: list[Use] = field(default_factory=list)


@dataclass
class RemoveResult:
    deleted: list[str] = field(default_factory=list)  # visual names removed, the visual first
    parts: set[str] = field(default_factory=set)  # parts changed (page, bookmarks)
    counts: dict[str, int] = field(default_factory=dict)  # cleaned entries per USE_KIND


# --- reading the document's JSON parts ---------------------------------------------------


class _Part(NamedTuple):
    name: str
    data: Any
    kind: str  # report | pages | page | visual | bookmark | extension
    owned: bool  # the document tracks and writes this part itself (``mark_dirty``)


def _context(name: str) -> tuple[str | None, str | None]:
    """The page and visual a part belongs to."""
    visual = _VISUAL_PART.fullmatch(name)
    if visual:
        return visual.group(1), visual.group(2)
    page = _PAGE_PART.fullmatch(name)
    return (page.group(1) if page else None), None


def _walk(document: PbirDocument) -> list[_Part]:
    """report.json, pages.json, every page.json and visual.json, bookmarks and extensions.

    These are the document's live dicts (``iter_json_parts``): change one in place and
    ``_persist`` it. Report-level extension measures are not tracked by the document, so
    they are persisted straight into the package.
    """
    parts = [_Part(name, data, kind, True) for name, data, kind in document.iter_json_parts()]
    extensions = document.extensions
    if extensions:
        parts.append(_Part(EXTENSIONS, extensions, "extension", False))
    return parts


def _persist(document: PbirDocument, part: _Part) -> None:
    if part.owned:
        document.mark_dirty(part.name)
    else:
        document.package.write_json(part.name, part.data)


def _dax_queries(document: PbirDocument) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for name in document.package.names():
        if name.startswith("DAXQueries/") and name.lower().endswith(".dax"):
            try:
                found.append((name, _decode_text(document.package.read(name))))
            except (KeyError, PackageError):
                continue
    return found


def _decode_text(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:16]:
        return data.decode("utf-16-le", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _pointer(path: list[Any]) -> str:
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _literal_text(prop: Any) -> str | None:
    """The string a literal property holds, else None."""
    if not isinstance(prop, dict):
        return None
    expr = prop.get("expr")
    literal = expr.get("Literal") if isinstance(expr, dict) else None
    value = literal.get("Value") if isinstance(literal, dict) else None
    if isinstance(value, str) and len(value) >= 2 and value[0] == "'" and value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return None


# --- field expressions --------------------------------------------------------------------

_Sink = Callable[[str, dict[str, Any], dict[str, Any], dict[str, Any], list[Any], str | None], None]


def _entity(body: dict[str, Any], scopes: list[dict[str, str]]) -> str | None:
    expression = body.get("Expression")
    source = expression.get("SourceRef") if isinstance(expression, dict) else None
    if not isinstance(source, dict):
        return None
    entity = source.get("Entity")
    if isinstance(entity, str):
        return entity
    alias = source.get("Source")
    if isinstance(alias, str):
        for scope in reversed(scopes):
            if alias in scope:
                return scope[alias]
    return None


def _scan(
    node: Any,
    path: list[Any],
    scopes: list[dict[str, str]],
    parent: dict[str, Any] | None,
    sink: _Sink,
) -> None:
    """Call ``sink`` for every Measure and Column expression under ``node``.

    A dict with a ``From`` list opens a scope that maps its aliases to entities, so a
    ``Source`` resolves through the nearest enclosing ``From`` (a subquery has its own).
    """
    if isinstance(node, list):
        for i, item in enumerate(node):
            _scan(item, [*path, i], scopes, None, sink)
        return
    if not isinstance(node, dict):
        return
    pushed = False
    sources = node.get("From")
    if isinstance(sources, list):
        aliases = {
            e["Name"]: e["Entity"]
            for e in sources
            if isinstance(e, dict)
            and isinstance(e.get("Name"), str)
            and isinstance(e.get("Entity"), str)
        }
        scopes.append(aliases)
        pushed = True
    for kind in ("Measure", "Column"):
        body = node.get(kind)
        if (
            isinstance(body, dict)
            and isinstance(body.get("Property"), str)
            and isinstance(body.get("Expression"), dict)
        ):
            projection = parent if path and path[-1] == "field" and parent is not None else node
            sink(kind, body, node, projection, [*path, kind], _entity(body, scopes))
    for key, value in node.items():
        _scan(value, [*path, key], scopes, node, sink)
    if pushed:
        scopes.pop()


def _use_kind(part_kind: str, path: list[Any]) -> str:
    if part_kind == "bookmark":
        return "bookmark"
    if part_kind == "extension":
        return "measure_dax"
    if "filterConfig" in path:
        if part_kind == "page":
            return "page_filter"
        if part_kind == "report":
            return "report_filter"
        rest = path[path.index("filterConfig") + 3 :]
        return "filter_field" if rest[:1] == ["field"] else "filter_where"
    if part_kind == "page" and "pageBinding" in path:
        return "page_binding"
    if "sortDefinition" in path:
        return "sort"
    if "selector" in path:
        return "selector"
    if "queryState" in path:
        return "projection"
    return "object_expression"


def _filter_index(path: list[Any]) -> int | None:
    if "filterConfig" not in path:
        return None
    rest = path[path.index("filterConfig") + 1 :]
    if len(rest) >= 2 and rest[0] == "filters" and isinstance(rest[1], int):
        return rest[1]
    return None


def _first_field(node: Any) -> tuple[str, str | None, str] | None:
    found: list[tuple[str, str | None, str]] = []

    def sink(
        kind: str,
        body: dict[str, Any],
        holder: dict[str, Any],
        projection: dict[str, Any],
        path: list[Any],
        entity: str | None,
    ) -> None:
        if not found:
            found.append((kind, entity, str(body["Property"])))

    _scan(node, [], [], None, sink)
    return found[0] if found else None


def _query_refs(visual: dict[str, Any]) -> dict[str, tuple[str, str | None, str]]:
    """queryRef -> field, from a visual's projections (selector metadata uses queryRefs)."""
    refs: dict[str, tuple[str, str | None, str]] = {}
    state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    for role in state.values() if isinstance(state, dict) else []:
        for projection in role.get("projections", []) if isinstance(role, dict) else []:
            if isinstance(projection, dict) and isinstance(projection.get("queryRef"), str):
                identity = _first_field(projection.get("field"))
                if identity:
                    refs[projection["queryRef"]] = identity
    return refs


# --- readable names -----------------------------------------------------------------------

_TYPE_WORDS = {
    "tableEx": "table",
    "pivotTable": "matrix",
    "cardVisual": "card",
    "multiRowCard": "multi-row card",
    "advancedSlicerVisual": "slicer",
    "textbox": "text box",
    "actionButton": "button",
    "visualGroup": "group",
    "kpi": "KPI",
}
_WHAT = {
    "projection": "field",
    "object_expression": "formatting",
    "selector": "formatting rule",
    "sort": "sort",
    "filter_field": "filter",
    "filter_where": "filter",
    "page_filter": "page filter",
    "report_filter": "report filter",
    "bookmark": "bookmark",
}


def _describe_visual(visual: dict[str, Any], name: str) -> str:
    """'table 'Agent ID'' or 'card 2202e311': what a person would call the visual."""
    inner: dict[str, Any] = visual["visual"] if isinstance(visual.get("visual"), dict) else {}
    group = visual.get("visualGroup")
    raw_type = "visualGroup" if isinstance(group, dict) else str(inner.get("visualType", "visual"))
    words = _TYPE_WORDS.get(raw_type) or re.sub(r"(?<!^)(?=[A-Z])", " ", raw_type).lower()
    text: str | None = None
    if isinstance(group, dict):
        text = str(group.get("displayName") or "") or None
    for holder, names in (
        (inner.get("visualContainerObjects"), ("title",)),
        (inner.get("objects"), ("header", "label", "text")),
    ):
        if text or not isinstance(holder, dict):
            continue
        for object_name in names:
            for entry in holder.get(object_name) or []:
                props = entry.get("properties") if isinstance(entry, dict) else None
                text = _literal_text(props.get("text")) if isinstance(props, dict) else None
                if text and text.strip():
                    break
            if text and text.strip():
                break
    if not (text and text.strip()):
        text = None
        state = inner.get("query", {}).get("queryState", {}) if inner else {}
        for role in state.values() if isinstance(state, dict) else []:
            projections = role.get("projections") if isinstance(role, dict) else None
            if projections and isinstance(projections[0], dict):
                first = projections[0]
                found = _first_field(first.get("field"))
                text = (
                    str(first.get("displayName") or "").strip()
                    or str(first.get("nativeQueryRef") or "").strip()
                    or (found[2] if found else "")
                ) or None
                break
    return f"{words} '{text.strip()}'" if text else f"{words} {name[:8]}"


def _plural(count: int, noun: str) -> str:
    if count == 1:
        return f"1 {noun}"
    return f"{count} {noun[:-1] + 'ies' if noun.endswith('y') else noun + 's'}"


def _names(labels: list[str]) -> str:
    shown = ", ".join(labels[:3])
    return shown + (f" and {len(labels) - 3} more" if len(labels) > 3 else "")


# --- DAX references -----------------------------------------------------------------------


@dataclass(frozen=True)
class DaxRef:
    """A name reference in DAX text.

    ``Table[Name]`` has both parts, a bare ``[Name]`` has no table, and a table used on its
    own (``'Field Parameter'`` as an argument) has no name. ``start``/``end`` span the
    bracket token, or the table token when there is no name.
    """

    table: str | None
    name: str | None
    start: int
    end: int


class _Tok(NamedTuple):
    kind: str  # string | quoted | bracket | word | other
    text: str
    start: int
    end: int


_WORD = re.compile(r"[^\W\d]\w*")
# Words that can directly precede a bracket without being a table name: RETURN [Measure].
_NOT_TABLE = frozenset(
    {"RETURN", "IN", "NOT", "AND", "OR", "EVALUATE", "DEFINE", "BY", "ASC", "DESC", "ELSE", "THEN"}
)


def _tokenize(text: str) -> list[_Tok]:
    """DAX tokens without comments. Never raises: malformed text just ends a token early."""
    tokens: list[_Tok] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        pair = text[i : i + 2]
        if char.isspace():
            i += 1
        elif pair in ("//", "--"):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif pair == "/*":
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char in "\"'[":
            closer = "]" if char == "[" else char
            j = i + 1
            while j < n:
                if text[j] == closer:
                    if text[j + 1 : j + 2] == closer:  # doubled quote or ]] is an escape
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            body = text[i + 1 : j].replace(closer * 2, closer)
            kind = "string" if char == '"' else "quoted" if char == "'" else "bracket"
            tokens.append(_Tok(kind, body, i, end))
            i = end
        else:
            match = _WORD.match(text, i)
            if match:
                tokens.append(_Tok("word", match.group(), i, match.end()))
                i = match.end()
            else:
                tokens.append(_Tok("other", char, i, i + 1))
                i += 1
    return tokens


def dax_references(expression: str, *, tables: bool = False) -> list[DaxRef]:
    """Every measure and column name the DAX text refers to (``Table[Name]``, ``[Name]``).

    With ``tables=True`` a table used on its own (``COUNTROWS('Field Parameter')``) is
    listed too, as a ref without a name; every identifier that is not a function call
    qualifies, so callers match those against the table names they know.

    Strings and comments are skipped. This is a name scan, not a parse, so it works on
    any DAX including syntax the local engine cannot evaluate.
    """
    tokens = _tokenize(expression)
    refs: list[DaxRef] = []
    for i, token in enumerate(tokens):
        following = tokens[i + 1] if i + 1 < len(tokens) else None
        if token.kind == "bracket":
            previous = tokens[i - 1] if i else None
            table: str | None = None
            if previous is not None and (
                previous.kind == "quoted"
                or (
                    previous.kind == "word"
                    and previous.end == token.start
                    and previous.text.upper() not in _NOT_TABLE
                )
            ):
                table = previous.text
            refs.append(DaxRef(table, token.text, token.start, token.end))
        elif tables and token.kind in ("quoted", "word"):
            if (
                following is not None
                and following.kind == "bracket"
                and (token.kind == "quoted" or following.start == token.end)
            ):
                continue
            if token.kind == "word" and following is not None and following.text == "(":
                continue
            refs.append(DaxRef(token.text, None, token.start, token.end))
    return refs


def rename_dax_references(expression: str, table: str, old: str, new: str) -> tuple[str, int]:
    """DAX text with ``[old]`` and ``Table[old]`` renamed; returns it and the rewrite count.

    A bare ``[old]`` counts as the measure (measure names are unique in a model); a
    reference qualified with another table is a column of that table and stays.
    """
    pieces: list[str] = []
    last = count = 0
    for ref in dax_references(expression):
        if ref.name is None or ref.name.casefold() != old.casefold():
            continue
        if ref.table is not None and ref.table.casefold() != table.casefold():
            continue
        pieces.append(expression[last : ref.start])
        pieces.append("[" + new.replace("]", "]]") + "]")
        last = ref.end
        count += 1
    pieces.append(expression[last:])
    return "".join(pieces), count


def _split_key(key: str) -> tuple[str, str | None]:
    """'Table[Column]' / "'My Table'[Column]" -> (table, column); a lone name -> (name, None)."""
    # Not tokenized: an unquoted table name may hold an apostrophe ("Bob's Table[M]"), which
    # the tokenizer would read as the start of a quoted name that swallows the bracket.
    text = key.strip()
    if text.endswith("]"):
        start = -1
        if text.startswith("'"):
            i = 1
            while i < len(text):
                if text[i] == "'":
                    if text[i + 1 : i + 2] == "'":
                        i += 2
                        continue
                    if text[i + 1 : i + 2] == "[":
                        start = i + 1
                    break
                i += 1
        if start < 0:
            start = text.find("[")
        if start >= 0:
            prefix = text[:start].strip()
            if len(prefix) >= 2 and prefix[0] == "'" and prefix[-1] == "'":
                prefix = prefix[1:-1].replace("''", "'")
            return prefix, text[start + 1 : -1].replace("]]", "]")
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("''", "'"), None
    return text, None


@dataclass(frozen=True)
class DaxItem:
    """A measure, calculated column or calculated table whose DAX can name a measure."""

    kind: str  # measure | column | table
    name: str  # measure name, "Table[Column]" or table name
    table: str | None
    expression: str
    part: str
    where: str
    field_parameter: bool = False

    @property
    def use_kind(self) -> str:
        if self.kind == "measure":
            return "measure_dax"
        if self.kind == "column":
            return "calculated_column"
        return "field_parameter" if self.field_parameter else "calculated_table"

    @property
    def node(self) -> tuple[str, str]:
        return (self.kind, self.name.casefold())

    def describe(self) -> str:
        noun = {"measure": "measure", "column": "calculated column"}.get(self.kind)
        if noun is None:
            noun = "field parameter" if self.field_parameter else "calculated table"
        return f"the {noun} '{self.name}'"


# --- the index ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Field:
    kind: str  # Measure | Column
    entity: str | None
    prop: str
    use: Use
    visual: str | None


@dataclass(frozen=True)
class _Selector:
    use: Use
    visual: str
    target: tuple[str, str | None, str] | None  # the projection the metadata points at
    text: str


class ReferenceIndex:
    """Where a report refers to measures, visuals, pages and filters. Build, then query."""

    def __init__(self) -> None:
        self._fields: list[_Field] = []
        self._selectors: list[_Selector] = []
        self._queries: list[tuple[str, str]] = []
        self._items: dict[tuple[str, str], DaxItem] = {}
        self._rdeps: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self._visual_uses: dict[str, list[Use]] = {}
        self._page_uses: dict[str, list[Use]] = {}
        self._filter_uses: dict[str, list[Use]] = {}
        self._bookmark_uses: dict[str, list[Use]] = {}
        self._page_names: dict[str, str] = {}
        self._visual_desc: dict[str, str] = {}
        self._visual_order: list[str] = []

    # --- building --------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        document: PbirDocument,
        *,
        measures: Mapping[str, str] | None = None,
        calculated: Mapping[str, str] | None = None,
    ) -> ReferenceIndex:
        """Index ``document``.

        ``measures`` maps each model measure to its DAX (keys ``Name`` or ``Table[Name]``);
        ``calculated`` maps ``Table[Column]`` (calculated column) or ``Table`` (calculated
        table, field parameter) to its DAX. Both are optional: a thin report has neither.
        """
        index = cls()
        parts = _walk(document)
        for part in parts:
            page, visual = _context(part.name)
            if part.kind == "page" and page:
                index._page_names[page] = str(part.data.get("displayName") or page)
            elif part.kind == "visual" and visual:
                index._visual_desc[visual] = _describe_visual(part.data, visual)
                index._visual_order.append(visual)
        for part in parts:
            index._scan_part(part)
        index._queries = _dax_queries(document)
        index._build_graph(parts, measures or {}, calculated or {})
        return index

    def _label(self, part: _Part, what: str) -> str:
        page, visual = _context(part.name)
        if visual:
            return f"{self._visual_desc.get(visual, visual)} ({what})"
        if page:
            return f"page '{self._page_names.get(page, page)}' ({what})"
        if part.kind == "bookmark":
            name = part.data.get("displayName") or part.data.get("name") or "bookmark"
            return f"bookmark '{name}'"
        return f"the report ({what})"

    def _scan_part(self, part: _Part) -> None:
        if part.kind not in ("report", "page", "visual", "bookmark") or not isinstance(
            part.data, dict
        ):
            return
        page, visual = _context(part.name)

        def sink(
            kind: str,
            body: dict[str, Any],
            holder: dict[str, Any],
            projection: dict[str, Any],
            path: list[Any],
            entity: str | None,
        ) -> None:
            use_kind = _use_kind(part.kind, path)
            use = Use(
                use_kind,
                part.name,
                _pointer(path),
                self._label(part, _WHAT.get(use_kind, "binding")),
            )
            self._fields.append(_Field(kind, entity, str(body["Property"]), use, visual))

        _scan(part.data, [], [], None, sink)
        self._scan_filter_definitions(part)
        if part.kind == "visual" and visual and page:
            self._scan_visual(part, page, visual)
        elif part.kind == "page" and page:
            self._scan_page(part, page)
        elif part.kind == "bookmark":
            self._scan_bookmark(part)

    def _scan_filter_definitions(self, part: _Part) -> None:
        config = part.data.get("filterConfig")
        filters = config.get("filters") if isinstance(config, dict) else None
        if not isinstance(filters, list):
            return
        kind = {"page": "page_filter", "report": "report_filter"}.get(part.kind, "filter_field")
        for i, item in enumerate(filters):
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                use = Use(
                    kind,
                    part.name,
                    f"/filterConfig/filters/{i}/name",
                    self._label(part, _WHAT[kind]),
                )
                self._filter_uses.setdefault(item["name"], []).append(use)

    def _scan_visual(self, part: _Part, page: str, visual: str) -> None:
        data = part.data
        refs = _query_refs(data)
        self._collect_selectors(data, part, visual, refs, [], data)
        group = data.get("parentGroupName")
        if isinstance(group, str):
            self._visual_uses.setdefault(group, []).append(
                Use(
                    "group_member",
                    part.name,
                    "/parentGroupName",
                    f"{self._visual_desc.get(visual, visual)} is in this group",
                )
            )
        inner = data.get("visual") if isinstance(data.get("visual"), dict) else {}
        for holder_key in ("objects", "visualContainerObjects"):
            holder = inner.get(holder_key)
            if not isinstance(holder, dict):
                continue
            for object_name, prop_names in (
                (
                    "visualLink",
                    (("navigationSection", "visual_link"), ("drillthroughSection", "drillthrough")),
                ),
                ("visualTooltip", (("section", "tooltip"),)),
            ):
                for i, entry in enumerate(holder.get(object_name) or []):
                    props = entry.get("properties") if isinstance(entry, dict) else None
                    if not isinstance(props, dict):
                        continue
                    for prop, kind in prop_names:
                        target = _literal_text(props.get(prop))
                        if target:
                            phrase = {
                                "visual_link": "navigates to this page",
                                "drillthrough": "drills through to this page",
                                "tooltip": "uses this page as its tooltip",
                            }[kind]
                            self._page_uses.setdefault(target, []).append(
                                Use(
                                    kind,
                                    part.name,
                                    _pointer(
                                        ["visual", holder_key, object_name, i, "properties", prop]
                                    ),
                                    f"{self._visual_desc.get(visual, visual)} {phrase}",
                                )
                            )
        for found in _bookmark_links(data):
            self._bookmark_uses.setdefault(found.target, []).append(
                Use(
                    "bookmark_action" if found.kind == "button" else "bookmark_navigator",
                    part.name,
                    found.pointer,
                    f"{self._visual_desc.get(visual, visual)} {found.phrase}",
                )
            )

    def _collect_selectors(
        self,
        node: Any,
        part: _Part,
        visual: str,
        refs: dict[str, tuple[str, str | None, str]],
        path: list[Any],
        root: dict[str, Any],
    ) -> None:
        if isinstance(node, list):
            for i, item in enumerate(node):
                self._collect_selectors(item, part, visual, refs, [*path, i], root)
        elif isinstance(node, dict):
            selector = node.get("selector")
            if isinstance(selector, dict) and isinstance(selector.get("metadata"), str):
                text = selector["metadata"]
                self._selectors.append(
                    _Selector(
                        Use(
                            "selector",
                            part.name,
                            _pointer([*path, "selector", "metadata"]),
                            self._label(part, _WHAT["selector"]),
                        ),
                        visual,
                        refs.get(text),
                        text,
                    )
                )
            for key, value in node.items():
                self._collect_selectors(value, part, visual, refs, [*path, key], root)

    def _scan_page(self, part: _Part, page: str) -> None:
        data = part.data
        for i, item in enumerate(data.get("visualInteractions") or []):
            if not isinstance(item, dict):
                continue
            for role in ("source", "target"):
                name = item.get(role)
                if isinstance(name, str):
                    self._visual_uses.setdefault(name, []).append(
                        Use(
                            "visual_interaction",
                            part.name,
                            f"/visualInteractions/{i}",
                            f"an interaction on page '{self._page_names.get(page, page)}'",
                        )
                    )
        binding = data.get("pageBinding")
        if isinstance(binding, dict):
            kind = str(binding.get("type") or "binding").lower()
            self._page_uses.setdefault(page, []).append(
                Use("page_binding", part.name, "/pageBinding", f"this page is a {kind} page")
            )
            for i, param in enumerate(binding.get("parameters") or []):
                bound = param.get("boundFilter") if isinstance(param, dict) else None
                if isinstance(bound, str):
                    self._filter_uses.setdefault(bound, []).append(
                        Use(
                            "bound_filter",
                            part.name,
                            f"/pageBinding/parameters/{i}/boundFilter",
                            f"the {kind} setting of page '{self._page_names.get(page, page)}'",
                        )
                    )

    def _scan_bookmark(self, part: _Part) -> None:
        data = part.data
        label = self._label(part, "bookmark")
        state = data.get("explorationState")
        if not isinstance(state, dict):
            return

        def add(table: dict[str, list[Use]], name: str, where: list[Any]) -> None:
            table.setdefault(name, []).append(Use("bookmark", part.name, _pointer(where), label))

        active = state.get("activeSection")
        if isinstance(active, str):
            add(self._page_uses, active, ["explorationState", "activeSection"])
        sections = state.get("sections")
        for page, section in sections.items() if isinstance(sections, dict) else []:
            add(self._page_uses, page, ["explorationState", "sections", page])
            if not isinstance(section, dict):
                continue
            for key in ("visualContainers", "visualContainerGroups"):
                members = section.get(key)
                for visual in members if isinstance(members, dict) else []:
                    add(
                        self._visual_uses,
                        visual,
                        ["explorationState", "sections", page, key, visual],
                    )
        targets = data.get("options", {}).get("targetVisualNames")
        for i, visual in enumerate(targets if isinstance(targets, list) else []):
            if isinstance(visual, str):
                add(self._visual_uses, visual, ["options", "targetVisualNames", i])
        for where, name in _bookmark_filter_names(state, ["explorationState"]):
            add(self._filter_uses, name, where)

    def _build_graph(
        self, parts: list[_Part], measures: Mapping[str, str], calculated: Mapping[str, str]
    ) -> None:
        items: list[DaxItem] = []
        for key, expression in measures.items():
            table, name = _split_key(key)
            measure = name if name is not None else table
            items.append(
                DaxItem(
                    "measure",
                    measure,
                    (table or None) if name is not None else None,
                    expression,
                    MODEL_PART,
                    f"/measures/{measure}",
                )
            )
        known = {i.name.casefold() for i in items}
        for part in parts:
            if part.kind != "extension" or not isinstance(part.data, dict):
                continue
            for e, entity in enumerate(part.data.get("entities") or []):
                if not isinstance(entity, dict):
                    continue
                for m, measure in enumerate(entity.get("measures") or []):
                    if not isinstance(measure, dict) or not isinstance(measure.get("name"), str):
                        continue
                    if measure["name"].casefold() in known:
                        continue
                    owner = entity.get("name")
                    items.append(
                        DaxItem(
                            "measure",
                            measure["name"],
                            owner if isinstance(owner, str) else None,
                            str(measure.get("expression") or ""),
                            part.name,
                            f"/entities/{e}/measures/{m}/expression",
                        )
                    )
        for key, expression in calculated.items():
            table, column = _split_key(key)
            if column is not None:
                items.append(
                    DaxItem(
                        "column",
                        f"{table}[{column}]",
                        table,
                        expression,
                        MODEL_PART,
                        f"/columns/{key}",
                    )
                )
            else:
                parameter = any(
                    t.kind == "word" and t.text.upper() == "NAMEOF" for t in _tokenize(expression)
                )
                items.append(
                    DaxItem(
                        "table", table, table, expression, MODEL_PART, f"/tables/{table}", parameter
                    )
                )
        for item in items:
            self._items.setdefault(item.node, item)
        for item in list(self._items.values()):
            for ref in dax_references(item.expression, tables=True):
                target = self._resolve(ref, item)
                if target is not None and target != item.node:
                    dependents = self._rdeps.setdefault(target, [])
                    if item.node not in dependents:
                        dependents.append(item.node)

    def _resolve(self, ref: DaxRef, owner: DaxItem) -> tuple[str, str] | None:
        if ref.name is None:
            node = ("table", (ref.table or "").casefold())
            return node if node in self._items else None
        name = ref.name.casefold()
        if ref.table is None:
            if ("measure", name) in self._items:
                return ("measure", name)
            own = owner.table
            if own is not None and ("column", f"{own}[{ref.name}]".casefold()) in self._items:
                return ("column", f"{own}[{ref.name}]".casefold())
            matches = [k for k in self._items if k[0] == "column" and k[1].endswith(f"[{name}]")]
            return matches[0] if len(matches) == 1 else None
        table = ref.table.casefold()
        measure = self._items.get(("measure", name))
        if measure is not None and (measure.table is None or measure.table.casefold() == table):
            return ("measure", name)
        column = ("column", f"{ref.table}[{ref.name}]".casefold())
        if column in self._items:
            return column
        node = ("table", table)
        return node if node in self._items else None

    # --- measures ----------------------------------------------------------------------

    def _dependent_nodes(self, name: str) -> list[tuple[str, str]]:
        """Nodes whose DAX depends on the measure, directly or through others."""
        start = ("measure", name.casefold())
        seen = {start}
        order: list[tuple[str, str]] = []
        queue = [start]
        while queue:
            current = queue.pop(0)
            for node in self._rdeps.get(current, []):
                if node not in seen:
                    seen.add(node)
                    order.append(node)
                    queue.append(node)
        return order

    def dependent_items(self, name: str) -> list[DaxItem]:
        """The measures, calculated columns and tables that depend on a measure."""
        rank = {"measure": 0, "column": 1, "table": 2}
        items = [self._items[n] for n in self._dependent_nodes(name)]
        return sorted(items, key=lambda i: (rank[i.kind], i.name.casefold()))

    def measure_dependents(self, name: str) -> list[str]:
        """Measures ('Name'), columns ('Table[Column]') and tables whose DAX needs it."""
        return [i.name for i in self.dependent_items(name)]

    @staticmethod
    def _table_matches(entity: str | None, table: str | None) -> bool:
        return table is None or (entity is not None and entity.casefold() == table.casefold())

    def measure_uses(self, name: str, table: str | None = None) -> list[Use]:
        """Direct uses only: fields, selectors, DAX queries and the DAX of other items."""
        wanted = name.casefold()
        uses = [
            f.use
            for f in self._fields
            if f.kind == "Measure"
            and f.prop.casefold() == wanted
            and self._table_matches(f.entity, table)
        ]
        for selector in self._selectors:
            target = selector.target
            if target is not None:
                hit = (
                    target[0] == "Measure"
                    and target[2].casefold() == wanted
                    and self._table_matches(target[1], table)
                )
            else:
                text = selector.text.casefold()
                hit = (
                    text == f"{table}.{name}".casefold()
                    if table is not None
                    else text.endswith("." + wanted)
                )
            if hit:
                uses.append(selector.use)
        for query, text in self._queries:
            for ref in dax_references(text):
                if (
                    ref.name is not None
                    and ref.name.casefold() == wanted
                    and (
                        ref.table is None
                        or table is None
                        or ref.table.casefold() == table.casefold()
                    )
                ):
                    uses.append(
                        Use(
                            "dax_query",
                            query,
                            f"/line/{text.count(chr(10), 0, ref.start) + 1}",
                            f"DAX query '{_query_name(query)}'",
                        )
                    )
        for node in self._rdeps.get(("measure", wanted), []):
            item = self._items[node]
            uses.append(Use(item.use_kind, item.part, item.where, item.describe()))
        return uses

    def visual_users_of_measure(self, name: str, table: str | None = None) -> list[str]:
        """Visuals that use the measure directly or through measures, columns or field
        parameters whose DAX needs it (in document order)."""
        wanted = name.casefold()
        dependents = self.dependent_items(name)
        measures = {i.name.casefold() for i in dependents if i.kind == "measure"}
        columns = {i.name.casefold() for i in dependents if i.kind == "column"}
        tables = {i.name.casefold() for i in dependents if i.kind == "table"}
        users: set[str] = set()
        for f in self._fields:
            if f.visual is None:
                continue
            entity = (f.entity or "").casefold()
            prop = f.prop.casefold()
            direct = f.kind == "Measure" and (
                (prop == wanted and self._table_matches(f.entity, table)) or prop in measures
            )
            through_column = f.kind == "Column" and f"{entity}[{prop}]" in columns
            if direct or through_column or entity in tables:
                users.add(f.visual)
        for s in self._selectors:
            if s.target is not None and s.target[0] == "Measure":
                prop = s.target[2].casefold()
                if (prop == wanted and self._table_matches(s.target[1], table)) or prop in measures:
                    users.add(s.visual)
        return [v for v in self._visual_order if v in users]

    # --- visuals, pages, filters -------------------------------------------------------

    def visual_uses(self, visual_name: str) -> list[Use]:
        """Interactions, bookmark states and target lists, and (for a group) its members.
        A tab order is a property of the visual, not a reference to it."""
        return list(self._visual_uses.get(visual_name, []))

    def page_uses(self, page_name: str) -> list[Use]:
        """Buttons, tooltips and drillthrough links, bookmarks, and the page's own binding."""
        return list(self._page_uses.get(page_name, []))

    def filter_uses(self, filter_name: str) -> list[Use]:
        """The filter's definition, bookmarks that store it, and drillthrough bindings."""
        return list(self._filter_uses.get(filter_name, []))

    def bookmark_uses(self, bookmark_name: str) -> list[Use]:
        """Buttons (shapes, pictures) whose action opens the bookmark, and bookmark navigators
        that open it on deselection or list it when it is a group id."""
        return list(self._bookmark_uses.get(bookmark_name, []))

    def visual_label(self, visual_name: str) -> str:
        return self._visual_desc.get(visual_name, visual_name)

    def page_label(self, page_name: str) -> str:
        return self._page_names.get(page_name, page_name)


def _query_name(part: str) -> str:
    return part.rsplit("/", 1)[-1].removesuffix(".dax").replace("%20", " ")


def _bookmark_filter_names(node: Any, path: list[Any]) -> Iterator[tuple[list[Any], str]]:
    if isinstance(node, dict):
        by_expr = node.get("byExpr")
        if isinstance(by_expr, list):
            for i, item in enumerate(by_expr):
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    yield [*path, "byExpr", i, "name"], item["name"]
        for key, value in node.items():
            yield from _bookmark_filter_names(value, [*path, key])
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _bookmark_filter_names(item, [*path, i])


# --- rename and delete of measures ------------------------------------------------------


def rename_measure_references(
    document: PbirDocument,
    table: str,
    old: str,
    new: str,
    *,
    index: ReferenceIndex | None = None,
) -> RenameResult:
    """Point every report reference to ``table``[``old``] at ``new``.

    Each SourceRef is resolved through its enclosing ``From`` list before matching, so a
    Top/Bottom-N filter that says ``Source: '#'`` is found too. Only the field's Property
    changes, plus nativeQueryRef when it equals the old name and the projection has no
    displayName. queryRef and selector metadata stay: Desktop keeps queryRef stable across
    a measure rename, and the selectors still point at it.
    """
    result = RenameResult()
    old_cf, table_cf = old.casefold(), table.casefold()
    for part in _walk(document):
        if part.kind not in ("report", "page", "visual", "bookmark", "extension"):
            continue
        before = sum(result.counts.values())

        def sink(
            kind: str,
            body: dict[str, Any],
            holder: dict[str, Any],
            projection: dict[str, Any],
            path: list[Any],
            entity: str | None,
            part: _Part = part,
        ) -> None:
            if (
                kind != "Measure"
                or entity is None
                or entity.casefold() != table_cf
                or str(body["Property"]).casefold() != old_cf
            ):
                return
            body["Property"] = new
            if projection.get("nativeQueryRef") == old and "displayName" not in projection:
                projection["nativeQueryRef"] = new
            if projection.get("NativeReferenceName") == old:
                projection["NativeReferenceName"] = new
            use_kind = _use_kind(part.kind, path)
            result.counts[use_kind] = result.counts.get(use_kind, 0) + 1

        if part.kind == "extension":
            _rename_in_extension(part.data, table, old, new, result)
        else:
            _scan(part.data, [], [], None, sink)
        if sum(result.counts.values()) != before:
            result.parts.add(part.name)
            _persist(document, part)
    if index is not None:
        result.unrewritten = [
            u
            for u in index.measure_uses(old, table)
            if u.kind in ("dax_query", *_DAX_KINDS) and u.part != EXTENSIONS
        ]
    return result


def _rename_in_extension(data: Any, table: str, old: str, new: str, result: RenameResult) -> None:
    """Report-level measures: rename the measure itself and rewrite DAX that names it."""
    count = 0
    for entity in data.get("entities") or [] if isinstance(data, dict) else []:
        if not isinstance(entity, dict):
            continue
        own = str(entity.get("name") or "")
        for measure in entity.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            if (
                own.casefold() == table.casefold()
                and str(measure.get("name", "")).casefold() == old.casefold()
            ):
                measure["name"] = new
                count += 1
            expression = measure.get("expression")
            if isinstance(expression, str):
                text, changed = rename_dax_references(expression, table, old, new)
                if changed:
                    measure["expression"] = text
                    count += changed
    if count:
        result.counts["measure_dax"] = result.counts.get("measure_dax", 0) + count


def delete_guard(index: ReferenceIndex, name: str, table: str | None = None) -> list[str]:
    """Why deleting the measure must be refused, in plain English; [] when nothing needs it.

    Each reason is a clause the caller can join: "used by 3 visuals: ...", "used in 2
    filters", "used by the measure 'X'", "needed indirectly by 1 visual: ...".
    """
    uses = index.measure_uses(name, table)
    reasons: list[str] = []
    direct: list[str] = []
    for u in uses:
        _, visual = _context(u.part)
        if visual and u.kind in _VISUAL_KINDS and visual not in direct:
            direct.append(visual)
    if direct:
        reasons.append(
            f"used by {_plural(len(direct), 'visual')}: "
            + _names([index.visual_label(v) for v in direct])
        )
    filters = {(u.part, _filter_index(_path_of(u.where))) for u in uses if u.kind in _FILTER_KINDS}
    if filters:
        reasons.append(f"used in {_plural(len(filters), 'filter')}")
    bookmarks = {u.part for u in uses if u.kind == "bookmark"}
    if bookmarks:
        reasons.append(f"used in {_plural(len(bookmarks), 'bookmark')}")
    queries = {u.part for u in uses if u.kind == "dax_query"}
    if queries:
        reasons.append(f"used in {_plural(len(queries), 'DAX query')}")
    # Formatting driven by a measure on a page or on the report itself (a background or
    # wallpaper colour) has no visual to name, but deleting the measure still breaks it.
    formatted_pages = {
        u.part
        for u in uses
        if u.kind in _VISUAL_KINDS and _context(u.part)[1] is None and _PAGE_PART.fullmatch(u.part)
    }
    if formatted_pages:
        reasons.append(f"used by the formatting of {_plural(len(formatted_pages), 'page')}")
    if any(
        u.kind in _VISUAL_KINDS and _context(u.part) == (None, None) and u.part != MODEL_PART
        for u in uses
    ):
        reasons.append("used by the formatting of the report")
    dependents = index.dependent_items(name)
    if dependents:
        reasons.append("used by " + _names([d.describe() for d in dependents]))
    indirect = [v for v in index.visual_users_of_measure(name, table) if v not in direct]
    if indirect:
        reasons.append(
            f"needed indirectly by {_plural(len(indirect), 'visual')}: "
            + _names([index.visual_label(v) for v in indirect])
        )
    return reasons


def _path_of(pointer: str) -> list[Any]:
    parts: list[Any] = []
    for piece in pointer.split("/")[1:]:
        piece = piece.replace("~1", "/").replace("~0", "~")
        parts.append(int(piece) if piece.isdigit() else piece)
    return parts


# --- visuals ---------------------------------------------------------------------------------


def _page_doc(document: PbirDocument, page: PageDoc | str) -> PageDoc:
    return page if isinstance(page, PageDoc) else document.page(page)


def _page_key(document: PbirDocument, key: str) -> str:
    if key in document.pages:
        return key
    try:
        return document.page(key).name
    except DocumentError:
        return key


def _mark(counts: dict[str, int], kind: str, amount: int = 1) -> None:
    counts[kind] = counts.get(kind, 0) + amount


def remove_visual_references(
    document: PbirDocument,
    page: PageDoc | str,
    visual_name: str,
    *,
    group_children: bool = True,
) -> RemoveResult:
    """Delete a visual and clear every reference to it; returns what was deleted.

    Removes the visual's interactions (as source or target) from the page, and its state
    and target-list entries from bookmarks. A group takes every descendant with it,
    including nested groups; with ``group_children=False`` the members are promoted to the
    group's own parent instead.
    """
    owner = _page_doc(document, page)
    if visual_name not in owner.visuals:
        raise DocumentError(f"There is no visual {visual_name!r} on page {owner.display_name!r}")
    result = RemoveResult()
    deleted = [visual_name]
    if group_children:
        for name in deleted:
            deleted.extend(
                n
                for n, v in owner.visuals.items()
                if v.get("parentGroupName") == name and n not in deleted
            )
    else:
        parent = owner.visuals[visual_name].get("parentGroupName")
        for name, visual in owner.visuals.items():
            if visual.get("parentGroupName") == visual_name:
                if isinstance(parent, str):
                    visual["parentGroupName"] = parent
                else:
                    visual.pop("parentGroupName", None)
                document.touch_visual(owner, name)
                result.parts.add(f"{owner.folder}visuals/{name}/visual.json")
    gone = set(deleted)
    interactions = owner.data.get("visualInteractions")
    if isinstance(interactions, list):
        kept = [
            i
            for i in interactions
            if not (isinstance(i, dict) and (i.get("source") in gone or i.get("target") in gone))
        ]
        if len(kept) != len(interactions):
            _mark(result.counts, "visual_interaction", len(interactions) - len(kept))
            if kept:
                owner.data["visualInteractions"] = kept
            else:
                owner.data.pop("visualInteractions")
            document.touch_page(owner)
            result.parts.add(owner.folder + "page.json")
    for part in _walk(document):
        if part.kind == "bookmark" and _drop_visuals_from_bookmark(part.data, owner.name, gone):
            _mark(result.counts, "bookmark")
            result.parts.add(part.name)
            _persist(document, part)
    for name in deleted:
        document.remove_visual(owner, name)
    result.deleted = deleted
    return result


def _drop_visuals_from_bookmark(data: Any, page: str, gone: set[str]) -> bool:
    changed = False
    state = data.get("explorationState") if isinstance(data, dict) else None
    sections = state.get("sections") if isinstance(state, dict) else None
    section = sections.get(page) if isinstance(sections, dict) else None
    if isinstance(section, dict):
        members = section.get("visualContainers")
        if isinstance(members, dict):
            for name in [n for n in members if n in gone]:
                del members[name]
                changed = True
        groups = section.get("visualContainerGroups")
        if isinstance(groups, dict):
            changed = _prune_group_tree(groups, gone) or changed
    options = data.get("options") if isinstance(data, dict) else None
    if isinstance(options, dict):
        targets = options.get("targetVisualNames")
        if isinstance(targets, list) and any(t in gone for t in targets):
            options["targetVisualNames"] = [t for t in targets if t not in gone]
            changed = True
    return changed


def _prune_group_tree(tree: dict[str, Any], gone: Collection[str], depth: int = 0) -> bool:
    """Drop the groups in ``gone`` from a bookmark's ``visualContainerGroups``, which is a tree
    (``{group: {isHidden, children: {...}}}``, nested groups under their parent). A surviving
    group under a dropped one moves up to its place, as the group's members move up to its
    parent when the group alone is deleted. Returns whether anything changed."""
    changed = False
    if depth > 32:  # a cycle cannot occur in a tree Desktop wrote; stop rather than recurse
        return False
    for name in list(tree):
        node = tree[name]
        children = node.get("children") if isinstance(node, dict) else None
        if isinstance(children, dict) and _prune_group_tree(children, gone, depth + 1):
            changed = True
            if not children:
                node.pop("children")
        if name in gone:
            del tree[name]
            changed = True
            for child, child_node in (children or {}).items():
                tree.setdefault(child, child_node)
    return changed


def _filter_names(document: PbirDocument) -> set[str]:
    names: set[str] = set()
    for part in _walk(document):
        config = part.data.get("filterConfig") if isinstance(part.data, dict) else None
        for item in config.get("filters", []) if isinstance(config, dict) else []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.add(item["name"])
        if part.kind == "bookmark":
            names.update(n for _, n in _bookmark_filter_names(part.data, []))
    return names


def _new_filter_name(document: PbirDocument, used: set[str]) -> str:
    """'Filter' + 20 hex characters, like Desktop; names are unique report-wide."""
    fresh = "Filter" + document.new_name({n.removeprefix("Filter") for n in used})
    used.add(fresh)
    return fresh


def _rename_filters(document: PbirDocument, data: dict[str, Any], used: set[str]) -> dict[str, str]:
    renamed: dict[str, str] = {}
    config = data.get("filterConfig")
    for item in config.get("filters", []) if isinstance(config, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            renamed[item["name"]] = _new_filter_name(document, used)
            item["name"] = renamed[item["name"]]
    return renamed


def remap_duplicate_visual(
    document: PbirDocument,
    source_page: PageDoc | str,
    new_visual: str | dict[str, Any],
    *,
    copied_groups: Collection[str] = (),
) -> None:
    """Make a copied visual a valid new one.

    It gets fresh 'Filter' + 20 hex names (they are unique report-wide), a tabOrder of
    max(existing tabOrder on its page) + 1000, and loses ``parentGroupName`` unless the
    group was copied along (``copied_groups``). ``new_visual`` is the copy's name, or its
    dict when it is not attached to a page yet (then it goes on ``source_page``).
    """
    target = _page_doc(document, source_page)
    visual: dict[str, Any] | None
    if isinstance(new_visual, str):
        name, visual = new_visual, None
        for candidate in document.pages.values():
            if name in candidate.visuals:
                target, visual = candidate, candidate.visuals[name]
        if visual is None:
            raise DocumentError(f"There is no visual {name!r} in this report")
    else:
        visual = new_visual
        name = str(visual.get("name", ""))
        for candidate in document.pages.values():
            if candidate.visuals.get(name) is visual:
                target = candidate
    used = _filter_names(document)
    _rename_filters(document, visual, used)
    others = [
        v.get("position", {}).get("tabOrder")
        for n, v in target.visuals.items()
        if n != name and isinstance(v.get("position"), dict)
    ]
    numbers = [o for o in others if isinstance(o, (int, float)) and not isinstance(o, bool)]
    visual.setdefault("position", {})["tabOrder"] = int(max(numbers) + 1000) if numbers else 0
    if visual.get("parentGroupName") not in copied_groups:
        visual.pop("parentGroupName", None)
    if name in target.visuals:
        document.touch_visual(target, name)


def _fresh_binding_name(document: PbirDocument) -> str:
    used: set[str] = set()
    for page in document.pages.values():
        binding = page.data.get("pageBinding")
        if isinstance(binding, dict) and isinstance(binding.get("name"), str):
            used.add(binding["name"])
    return document.new_name(used)


def remap_duplicate_page(
    document: PbirDocument, source_page: PageDoc | str, new_page: PageDoc
) -> dict[str, str]:
    """Make a copied page independent of its source; returns old -> new visual names.

    If ``new_page`` has no visuals yet they are copied from the source with new names.
    If it already has copies (same order as the source) they are paired in order, and any
    that reuse a name found elsewhere in the report are renamed. Then the page's
    visualInteractions follow the new names, every filter gets a new name, and a
    drillthrough or tooltip binding gets a new identity whose bound filters follow.
    """
    source = _page_doc(document, source_page)
    others = {n for p in document.pages.values() if p is not new_page for n in p.visuals}
    mapping: dict[str, str] = {}
    if not new_page.visuals:
        used = document.visual_names() | others
        for old_id, visual in source.visuals.items():
            fresh = document.new_name(used | set(mapping.values()))
            mapping[old_id] = fresh
            copy_ = copy.deepcopy(visual)
            copy_["name"] = fresh
            new_page.visuals[fresh] = copy_
    else:
        if len(new_page.visuals) != len(source.visuals):
            raise DocumentError("The copied page does not have the same visuals as its source")
        taken = others | set(source.visuals)
        rebuilt: dict[str, dict[str, Any]] = {}
        for (old_id, old), (new_id, new) in zip(
            source.visuals.items(), new_page.visuals.items(), strict=True
        ):
            if old.get("visual", {}).get("visualType") != new.get("visual", {}).get("visualType"):
                raise DocumentError("The copied page does not match its source")
            if new_id in taken or new_id in rebuilt:
                new_id = document.new_name(taken | set(rebuilt) | set(mapping.values()))
            new["name"] = new_id
            mapping[old_id] = new_id
            rebuilt[new_id] = new
        new_page.visuals = rebuilt
    used_filters = _filter_names(document)
    renamed: dict[str, str] = {}
    for new_id, visual in new_page.visuals.items():
        parent = visual.get("parentGroupName")
        if isinstance(parent, str) and parent in mapping:
            visual["parentGroupName"] = mapping[parent]
        renamed.update(_rename_filters(document, visual, used_filters))
        document.touch_visual(new_page, new_id)
    data = new_page.data
    data["name"] = new_page.name
    interactions = data.get("visualInteractions")
    if isinstance(interactions, list):
        data["visualInteractions"] = [
            {**i, "source": mapping[i["source"]], "target": mapping[i["target"]]}
            for i in interactions
            if isinstance(i, dict) and i.get("source") in mapping and i.get("target") in mapping
        ]
    page_filters = _rename_filters(document, data, used_filters)
    binding = data.get("pageBinding")
    if isinstance(binding, dict):
        binding["name"] = _fresh_binding_name(document)
        for param in binding.get("parameters") or []:
            if not isinstance(param, dict):
                continue
            bound = param.get("boundFilter")
            if isinstance(bound, str) and bound in page_filters:
                param["boundFilter"] = page_filters[bound]
                label = param.get("name")
                if isinstance(label, str) and bound in label:
                    param["name"] = label.replace(bound, page_filters[bound])
    document.touch_page(new_page)
    return mapping


# --- bookmarks -----------------------------------------------------------------------------


class _BookmarkLink(NamedTuple):
    kind: str  # button | navigator_deselection | navigator_group
    target: str  # the bookmark id (or bookmark group id for navigator_group)
    pointer: str
    phrase: str


def _bookmark_links(visual: Any) -> Iterator[_BookmarkLink]:
    """Where a visual names a bookmark: its action (``visualLink.bookmark``), and a bookmark
    navigator's ``deselectionBookmark`` and ``bookmarkGroup`` (a group id; "" is every
    bookmark)."""
    inner = visual.get("visual") if isinstance(visual, dict) else None
    if not isinstance(inner, dict):
        return
    for holder_key in ("visualContainerObjects", "objects"):
        holder = inner.get(holder_key)
        entries = holder.get("visualLink") if isinstance(holder, dict) else None
        for i, entry in enumerate(entries if isinstance(entries, list) else []):
            props = entry.get("properties") if isinstance(entry, dict) else None
            target = _literal_text(props.get("bookmark")) if isinstance(props, dict) else None
            if target:
                where = _pointer(["visual", holder_key, "visualLink", i, "properties", "bookmark"])
                yield _BookmarkLink("button", target, where, "opens this bookmark")
    objects = inner.get("objects")
    entries = objects.get("bookmarks") if isinstance(objects, dict) else None
    for i, entry in enumerate(entries if isinstance(entries, list) else []):
        props = entry.get("properties") if isinstance(entry, dict) else None
        if not isinstance(props, dict):
            continue
        for prop, kind, phrase in (
            ("deselectionBookmark", "navigator_deselection", "opens this bookmark on deselection"),
            ("bookmarkGroup", "navigator_group", "lists this bookmark group"),
        ):
            target = _literal_text(props.get(prop))
            if target:
                where = _pointer(["visual", "objects", "bookmarks", i, "properties", prop])
                yield _BookmarkLink(kind, target, where, phrase)


@dataclass(frozen=True)
class BookmarkReference:
    """Something that points at a bookmark (or a bookmark group) by its id."""

    kind: str  # button | navigator_deselection | navigator_group
    page: str
    visual: str
    who: str  # "button 'Go' on page '1'"
    label: str  # plain English: "button 'Go' on page '1' opens it"

    def naming(self, title: str) -> str:
        """The reference with the bookmark named: "button 'Go' on page '1' opens the
        bookmark 'Overview'"."""
        words = {
            "button": f"opens the bookmark '{title}'",
            "navigator_deselection": f"opens the bookmark '{title}' on deselection",
            "navigator_group": f"lists the bookmark group '{title}'",
        }[self.kind]
        return f"{self.who} {words}"


_REFERENCE_WORDS = {
    "button": "opens it",
    "navigator_deselection": "opens it on deselection",
    "navigator_group": "lists it",
}


def bookmark_references(document: PbirDocument, key: str) -> list[BookmarkReference]:
    """Buttons (and shapes or pictures) whose action opens the bookmark or group ``key``, and
    bookmark navigators that list the group or open the bookmark on deselection."""
    found: list[BookmarkReference] = []
    for page, name, visual in document.all_visuals():
        for link in _bookmark_links(visual):
            if link.target == key:
                who = f"{_describe_visual(visual, name)} on page '{page.display_name}'"
                label = f"{who} {_REFERENCE_WORDS[link.kind]}"
                found.append(BookmarkReference(link.kind, page.name, name, who, label))
    return found


def clear_bookmark_references(
    document: PbirDocument, key: str, title: str | None = None, *, skip_page: str | None = None
) -> list[str]:
    """Switch off what points at a bookmark about to be deleted (or a group about to go); returns
    what changed, in plain English. A button's action is switched off, a navigator no longer
    opens it on deselection, a navigator on the group lists every bookmark. Visuals on
    ``skip_page`` (a page being deleted) are left alone."""
    shown = title or key
    changes: list[str] = []
    for page, name, visual in document.all_visuals():
        if page.name == skip_page:
            continue
        inner = visual.get("visual")
        if not isinstance(inner, dict):
            continue
        touched = False
        for holder_key in ("visualContainerObjects", "objects"):
            holder = inner.get(holder_key)
            entries = holder.get("visualLink") if isinstance(holder, dict) else None
            for entry in entries if isinstance(entries, list) else []:
                props = entry.get("properties") if isinstance(entry, dict) else None
                if isinstance(props, dict) and _literal_text(props.get("bookmark")) == key:
                    props.pop("bookmark")
                    props["show"] = {"expr": {"Literal": {"Value": "false"}}}
                    touched = True
                    changes.append(
                        f"Switched off the action of {_describe_visual(visual, name)} on page "
                        f"'{page.display_name}', which opened the bookmark '{shown}'."
                    )
        objects = inner.get("objects")
        entries = objects.get("bookmarks") if isinstance(objects, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            props = entry.get("properties") if isinstance(entry, dict) else None
            if not isinstance(props, dict):
                continue
            label = f"{_describe_visual(visual, name)} on page '{page.display_name}'"
            if _literal_text(props.get("deselectionBookmark")) == key:
                props.pop("deselectionBookmark")
                props["allowDeselectionBookmark"] = {"expr": {"Literal": {"Value": "false"}}}
                touched = True
                changes.append(f"{label} no longer opens the bookmark '{shown}' on deselection.")
            if _literal_text(props.get("bookmarkGroup")) == key:
                props["bookmarkGroup"] = {"expr": {"Literal": {"Value": "''"}}}
                touched = True
                changes.append(f"{label} lists every bookmark again.")
        if touched:
            document.touch_visual(page, name)
    return changes


def _bookmark_key(part_name: str) -> str:
    return part_name.rsplit("/", 1)[-1].removesuffix(".bookmark.json")


def _drop_navigator_page(inner: dict[str, Any], page: str) -> bool:
    """A page navigator's own show/hide setting for one page (``pages.showPage`` under the
    selector ``{"id": page}``) goes with the page."""
    if inner.get("visualType") != "pageNavigator":
        return False
    objects = inner.get("objects")
    entries = objects.get("pages") if isinstance(objects, dict) else None
    if not isinstance(entries, list):
        return False
    kept = [e for e in entries if not (isinstance(e, dict) and e.get("selector") == {"id": page})]
    if len(kept) == len(entries):
        return False
    assert isinstance(objects, dict)
    if kept:
        objects["pages"] = kept
    else:
        objects.pop("pages")
    return True


def _deleted_with_page(state: Any, page: str) -> bool:
    """True when a bookmark remembers only this page, so deleting the page deletes it."""
    if not isinstance(state, dict):
        return False
    sections = state.get("sections")
    if isinstance(sections, dict) and sections:
        return set(sections) == {page}
    return state.get("activeSection") == page


# --- navigation references ---------------------------------------------------------------------
# What a bookmark, a button, a navigator, a tooltip or a drillthrough binding points at must
# exist and be of the right kind. The navigation family runs this after each of its edits, and
# the structural validator blocks a save on it; both read it here, where neither depends on the
# other.


@dataclass(frozen=True)
class NavigationFinding:
    """One dangling or inconsistent navigation reference: a stable ``code``, the part it is in,
    a JSON pointer inside that part, and plain English. The codes the structural validator
    checks on its own as well: ``bookmark_page_missing``, ``bookmark_visual_missing``,
    ``link_page_missing``, ``interaction_visual_missing``, ``bound_filter_missing``."""

    code: str
    part: str
    pointer: str
    message: str


# How a finding names a visual: (page, visual name, visual.json) -> "the 'Go' button".
Describe = Callable[[PageDoc, str, dict[str, Any]], str]
_Add = Callable[[str, str, tuple[Any, ...], str], None]


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _literal_value(prop: Any) -> str | None:
    """The raw literal of a property ("true", "'Back'", "2L"), else None."""
    expr = prop.get("expr") if isinstance(prop, dict) else None
    literal = expr.get("Literal") if isinstance(expr, dict) else None
    value = literal.get("Value") if isinstance(literal, dict) else None
    return value if isinstance(value, str) else None


def _list_in(holder: Any, name: str) -> list[Any]:
    """A list inside a JSON object as stored (positions intact), or []."""
    found = holder.get(name) if isinstance(holder, dict) else None
    return found if isinstance(found, list) else []


def _page_kind(page: PageDoc) -> str | None:
    """The page's kind: "Drillthrough", "Tooltip", or None for a normal page (its own type,
    else its binding's)."""
    kind = page.data.get("type")
    if kind in {"Drillthrough", "Tooltip"}:
        return str(kind)
    binding = page.data.get("pageBinding")
    if isinstance(binding, dict) and binding.get("type") in {"Drillthrough", "Tooltip"}:
        return str(binding["type"])
    return None


def _bookmark_title(bookmark: Mapping[str, Any], key: str) -> str:
    return f"'{bookmark.get('displayName') or key}'"


def _default_describe(page: PageDoc, name: str, visual: dict[str, Any]) -> str:
    return _describe_visual(visual, name)


def _bookmark_list_findings(document: PbirDocument, add: _Add) -> dict[str, list[Any]]:
    """The bookmark list against the bookmark files; returns where each id is listed."""
    bookmarks = document.bookmarks()
    items = document.bookmarks_metadata.get("items")
    listed: dict[str, list[Any]] = {}
    for index, item in enumerate(items if isinstance(items, list) else []):
        if not isinstance(item, dict):
            continue
        children = item.get("children")
        if isinstance(children, list):
            if not children:
                add(
                    "bookmark_group_empty",
                    BOOKMARKS_META,
                    ("items", index, "children"),
                    f"The bookmark group '{item.get('displayName') or item.get('name')}' is empty",
                )
            for position, child in enumerate(children):
                listed.setdefault(str(child), []).append(("items", index, "children", position))
        else:
            listed.setdefault(str(item.get("name")), []).append(("items", index))
    for key, paths in listed.items():
        if key not in bookmarks:
            add(
                "bookmark_list_missing",
                BOOKMARKS_META,
                paths[0],
                "The bookmark list names a bookmark that does not exist",
            )
            continue
        for path in paths[1:]:
            add(
                "bookmark_listed_twice",
                BOOKMARKS_META,
                path,
                f"The bookmark {_bookmark_title(bookmarks[key], key)} is listed more than once",
            )
    if isinstance(items, list):
        for key, bookmark in bookmarks.items():
            if key not in listed:
                add(
                    "bookmark_unlisted",
                    f"{BOOKMARKS}{key}.bookmark.json",
                    (),
                    f"The bookmark {_bookmark_title(bookmark, key)} is missing from the bookmark "
                    "list",
                )
    return listed


def _nested_group_findings(
    page: PageDoc, node: Any, path: tuple[Any, ...], label: str, part: str, add: _Add, depth: int
) -> None:
    """Groups inside a group of a bookmark's group tree must still be on the page."""
    children = node.get("children") if isinstance(node, dict) and depth < 16 else None
    for name, child in children.items() if isinstance(children, dict) else []:
        child_path = (*path, "children", name)
        if name not in page.visuals:
            add(
                "bookmark_group_missing",
                part,
                child_path,
                f"{label} remembers a group that is not on page '{page.display_name}'",
            )
        _nested_group_findings(page, child, child_path, label, part, add, depth + 1)


def _bookmark_state_findings(document: PbirDocument, add: _Add) -> None:
    """What each bookmark remembers: pages, visuals, nested groups, selected visuals."""
    every_visual = {name for page in document.pages.values() for name in page.visuals}
    for key, bookmark in document.bookmarks().items():
        part = f"{BOOKMARKS}{key}.bookmark.json"
        label = f"The bookmark {_bookmark_title(bookmark, key)}"
        state = bookmark.get("explorationState")
        state = state if isinstance(state, dict) else {}
        active = state.get("activeSection")
        if isinstance(active, str) and active not in document.pages:
            add(
                "bookmark_page_missing",
                part,
                ("explorationState", "activeSection"),
                f"{label} opens a page that does not exist",
            )
        sections = state.get("sections")
        for page_name, section in sections.items() if isinstance(sections, dict) else []:
            at = ("explorationState", "sections", page_name)
            if page_name not in document.pages:
                add(
                    "bookmark_page_missing",
                    part,
                    at,
                    f"{label} remembers a page that does not exist",
                )
                continue
            page = document.pages[page_name]
            gone = f"{label} remembers a visual that is not on page '{page.display_name}'"
            containers = section.get("visualContainers") if isinstance(section, dict) else None
            for name in containers if isinstance(containers, dict) else []:
                if name not in page.visuals:
                    add("bookmark_visual_missing", part, (*at, "visualContainers", name), gone)
            tree = section.get("visualContainerGroups") if isinstance(section, dict) else None
            for name, node in tree.items() if isinstance(tree, dict) else []:
                path = (*at, "visualContainerGroups", name)
                if name not in page.visuals:
                    add("bookmark_visual_missing", part, path, gone)
                _nested_group_findings(page, node, path, label, part, add, 0)
        options = bookmark.get("options")
        targets = options.get("targetVisualNames") if isinstance(options, dict) else None
        for index, name in enumerate(targets if isinstance(targets, list) else []):
            if name not in every_visual:
                add(
                    "bookmark_visual_missing",
                    part,
                    ("options", "targetVisualNames", index),
                    f"{label} applies to a visual that does not exist",
                )


def _link_findings(
    document: PbirDocument, part: str, label: str, inner: dict[str, Any], add: _Add
) -> None:
    """A button's (or shape's) action and a visual's tooltip page."""
    bookmarks = document.bookmarks()
    for holder_key in ("visualContainerObjects", "objects"):
        holder = inner.get(holder_key)
        for index, entry in enumerate(_list_in(holder, "visualLink")):
            props = entry.get("properties") if isinstance(entry, dict) else None
            if not isinstance(props, dict):
                continue
            at = ("visual", holder_key, "visualLink", index, "properties")
            target = _literal_text(props.get("bookmark"))
            if target and target not in bookmarks:
                add(
                    "link_bookmark_missing",
                    part,
                    (*at, "bookmark"),
                    f"{label} opens a bookmark that does not exist",
                )
            for prop in ("navigationSection", "drillthroughSection"):
                page_ref = _literal_text(props.get(prop))
                if page_ref and page_ref not in document.pages:
                    add(
                        "link_page_missing",
                        part,
                        (*at, prop),
                        f"{label} links to a page that does not exist",
                    )
            drill = _literal_text(props.get("drillthroughSection"))
            if (
                drill
                and drill in document.pages
                and _literal_value(props.get("show")) != "false"
                and _page_kind(document.pages[drill]) != "Drillthrough"
            ):
                add(
                    "link_not_drillthrough",
                    part,
                    (*at, "drillthroughSection"),
                    f"{label} drills through to a page that is not a drillthrough page",
                )
        for index, entry in enumerate(_list_in(holder, "visualTooltip")):
            props = entry.get("properties") if isinstance(entry, dict) else None
            section = _literal_text(props.get("section")) if isinstance(props, dict) else None
            if not section:
                continue
            tip = ("visual", holder_key, "visualTooltip", index, "properties", "section")
            if section not in document.pages:
                add(
                    "link_page_missing",
                    part,
                    tip,
                    f"{label} uses a tooltip page that does not exist",
                )
            elif _page_kind(document.pages[section]) != "Tooltip":
                add(
                    "tooltip_page_invalid",
                    part,
                    tip,
                    f"{label} uses a page that is not a tooltip page",
                )


def _navigator_findings(
    document: PbirDocument, part: str, label: str, inner: dict[str, Any], add: _Add
) -> None:
    """A bookmark navigator's group and deselection bookmark, a page navigator's per-page
    settings (``pages.showPage`` under ``{"id": page}``)."""
    groups = {
        str(i.get("name"))
        for i in _list_in(document.bookmarks_metadata, "items")
        if isinstance(i, dict) and isinstance(i.get("children"), list)
    }
    objects = inner.get("objects")
    for index, entry in enumerate(_list_in(objects, "bookmarks")):
        props = entry.get("properties") if isinstance(entry, dict) else None
        if not isinstance(props, dict):
            continue
        at = ("visual", "objects", "bookmarks", index, "properties")
        group = _literal_text(props.get("bookmarkGroup"))
        if group and group not in groups:
            add(
                "navigator_group_missing",
                part,
                (*at, "bookmarkGroup"),
                f"{label} lists a bookmark group that does not exist",
            )
        deselect = _literal_text(props.get("deselectionBookmark"))
        if deselect and deselect not in document.bookmarks():
            add(
                "navigator_bookmark_missing",
                part,
                (*at, "deselectionBookmark"),
                f"{label} opens a bookmark that does not exist when nothing is selected",
            )
    if inner.get("visualType") != "pageNavigator":
        return
    for index, entry in enumerate(_list_in(objects, "pages")):
        selector = entry.get("selector") if isinstance(entry, dict) else None
        page_ref = selector.get("id") if isinstance(selector, dict) else None
        if isinstance(page_ref, str) and page_ref not in document.pages:
            add(
                "navigator_page_missing",
                part,
                ("visual", "objects", "pages", index, "selector", "id"),
                f"{label} has a setting for a page that does not exist",
            )


def navigation_findings(
    document: PbirDocument, describe: Describe | None = None
) -> list[NavigationFinding]:
    """Every dangling or inconsistent navigation reference ([] when clean): the bookmark list
    against the bookmark files, what bookmarks remember (pages, visuals, nested groups), what
    buttons, navigators and tooltips point at and whether the page is of the right type,
    drillthrough and tooltip bindings, interactions, and sync groups that mix kinds of slicers.
    ``describe`` names a visual in the messages (default: its type and caption)."""
    found: list[NavigationFinding] = []
    name_of = describe or _default_describe

    def add(code: str, part: str, path: tuple[Any, ...], message: str) -> None:
        found.append(NavigationFinding(code, part, _pointer(list(path)), message))

    _bookmark_list_findings(document, add)
    _bookmark_state_findings(document, add)
    sync: dict[str, list[tuple[str, str]]] = {}  # group -> (kind, part) of each member
    for page in document.pages.values():
        page_part = page.folder + "page.json"
        where = f"page '{page.display_name}'"
        for index, item in enumerate(_list_in(page.data, "visualInteractions")):
            for role in ("source", "target"):
                if isinstance(item, dict) and item.get(role) not in page.visuals:
                    add(
                        "interaction_visual_missing",
                        page_part,
                        ("visualInteractions", index, role),
                        f"An interaction on {where} names a visual that is not there",
                    )
        filters = {
            f.get("name")
            for f in _list_in(page.data.get("filterConfig"), "filters")
            if isinstance(f, dict)
        }
        for index, param in enumerate(_list_in(page.data.get("pageBinding"), "parameters")):
            if isinstance(param, dict) and param.get("boundFilter") not in filters:
                add(
                    "bound_filter_missing",
                    page_part,
                    ("pageBinding", "parameters", index, "boundFilter"),
                    f"{_sentence(where)} is tied to a filter that is not on the page",
                )
        for name, visual in page.visuals.items():
            inner = visual.get("visual")
            if not isinstance(inner, dict):
                continue
            part = f"{page.folder}visuals/{name}/visual.json"
            label = _sentence(f"{name_of(page, name, visual)} on {where}")
            _link_findings(document, part, label, inner, add)
            _navigator_findings(document, part, label, inner, add)
            group = inner.get("syncGroup")
            if isinstance(group, dict) and isinstance(group.get("groupName"), str):
                kind = str(inner.get("visualType", ""))
                sync.setdefault(group["groupName"], []).append((kind, part))
    for group_name, members in sync.items():
        if len({kind for kind, _ in members}) > 1:
            add(
                "sync_group_mixed",
                members[-1][1],
                ("visual", "syncGroup", "groupName"),
                f"The sync group {group_name!r} mixes kinds of slicers",
            )
    return found


# --- pages ---------------------------------------------------------------------------------


def remove_page_references(document: PbirDocument, page_name: str) -> list[str]:
    """What still points at the page, in plain English; [] when it can be deleted cleanly.

    Buttons, tooltips and drillthrough links on other pages and bookmarks that open it, and
    what opens a bookmark that is deleted with the page. The caller refuses the delete, or asks
    and then calls ``clear_page_references``.
    """
    page = _page_key(document, page_name)
    index = ReferenceIndex.build(document)
    messages: list[str] = []
    seen_bookmarks: set[str] = set()
    for use in index.page_uses(page):
        owner, _ = _context(use.part)
        if use.kind == "page_binding" or owner == page:
            continue
        if use.kind == "bookmark":
            if use.part in seen_bookmarks:
                continue
            seen_bookmarks.add(use.part)
            messages.append(f"{use.label} opens it")
        else:
            messages.append(f"On page '{index.page_label(owner or '')}', {use.label}")
    for key, bookmark in document.bookmarks().items():
        if not _deleted_with_page(bookmark.get("explorationState"), page):
            continue
        title = str(bookmark.get("displayName") or key)
        for reference in bookmark_references(document, key):
            if reference.page != page:
                messages.append(reference.naming(title))
    return messages


def clear_page_references(document: PbirDocument, page_name: str) -> list[str]:
    """Cleanup for a page that is about to be deleted; returns what was changed.

    A button, drillthrough link or tooltip pointing at the page is switched off (its
    action entry is removed), and a page navigator forgets its own setting for the page. A
    bookmark loses the page's state, and is deleted when the page was its only one; what opens
    that bookmark is switched off first (``clear_bookmark_references``), and a group it leaves
    empty goes, its navigators listing every bookmark again.
    """
    page = _page_key(document, page_name)
    changes: list[str] = []
    parts = _walk(document)
    for part in parts:
        owner, visual = _context(part.name)
        if part.kind != "visual" or visual is None or owner == page:
            continue
        inner = part.data.get("visual")
        if not isinstance(inner, dict):
            continue
        cleared = _drop_navigator_page(inner, page)
        if cleared:
            changes.append(
                f"Removed the deleted page from the list of {_describe_visual(part.data, visual)}."
            )
        for holder_key in ("objects", "visualContainerObjects"):
            holder = inner.get(holder_key)
            if not isinstance(holder, dict):
                continue
            for object_name, keys in (
                ("visualLink", ("navigationSection", "drillthroughSection")),
                ("visualTooltip", ("section",)),
            ):
                for entry in holder.get(object_name) or []:
                    props = entry.get("properties") if isinstance(entry, dict) else None
                    if isinstance(props, dict) and any(
                        _literal_text(props.get(k)) == page for k in keys
                    ):
                        del holder[object_name]
                        cleared = True
                        what = "tooltip" if object_name == "visualTooltip" else "action"
                        changes.append(
                            f"Removed the {what} of {_describe_visual(part.data, visual)} "
                            f"that used the deleted page."
                        )
                        break
        if cleared:
            _persist(document, part)
    # Visual names are unique report-wide, so the page's own visuals can be dropped from any
    # bookmark's target list without touching another page's entries.
    doomed = set(document.pages[page].visuals) if page in document.pages else set()
    for part in parts:
        if part.kind != "bookmark" or not isinstance(part.data, dict):
            continue
        state = part.data.get("explorationState")
        sections = state.get("sections") if isinstance(state, dict) else None
        covers = isinstance(state, dict) and (
            state.get("activeSection") == page or (isinstance(sections, dict) and page in sections)
        )
        if not covers:
            if doomed and _drop_visuals_from_bookmark(part.data, page, doomed):
                _persist(document, part)
            continue
        assert isinstance(state, dict)
        if isinstance(sections, dict):
            sections.pop(page, None)
        title = part.data.get("displayName") or part.data.get("name") or "bookmark"
        if not sections:
            key = _bookmark_key(part.name)
            references = clear_bookmark_references(document, key, str(title), skip_page=page)
            unlisted = document.remove_bookmark(key)
            changes.append(
                f"Deleted the bookmark '{title}' because it only covered the deleted page."
            )
            changes.extend(references)
            for group in unlisted.groups:
                changes.append(f"Removed the bookmark group '{group}', which it left empty.")
            if unlisted.navigators:
                count = len(unlisted.navigators)
                changes.append(
                    f"{count} bookmark navigator{' lists' if count == 1 else 's list'} every "
                    "bookmark again."
                )
            continue
        if state.get("activeSection") == page:
            state["activeSection"] = next(iter(sections))
        if doomed:
            _drop_visuals_from_bookmark(part.data, page, doomed)
        _persist(document, part)
        changes.append(f"Removed the deleted page from the bookmark '{title}'.")
    return changes
