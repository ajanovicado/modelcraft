"""The caption a user sees on a visual, and where it is stored: one map for the importer,
the editor and the renderer.

A caption is not always the container title. Measured on the Agents Performance report
(Desktop 2.157): the new card ``2202e311`` shows "Agents Avg MTD Sales" from its label
(``objects.label`` scoped to the Data field) while its container has no title, and both
channel slicers show "Country" from ``objects.header`` although their field is
``ChannelName``. An index built from the container title or the first binding names those
visuals "Employees Avg MTD Sales" and "ChannelName", so "rename the Agents Avg MTD Sales
card" would not resolve. Every place that names, finds or renames a visual by what the
user reads goes through here.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from . import pbir_expr as expr

# What a caption bound to a field, measure or condition is called: its text is computed
# while the report runs, so Studio cannot know it (and must not overwrite it silently).
DYNAMIC_TITLE = "(dynamic title)"

Where = Literal["label", "header", "title", "paragraphs", "projection", "button_text"]
JsonPath = tuple[str | int, ...]

_TEXT_LIMIT = 120
# A text-only visual counts as a caption laid over another when this much of it is inside
# the other visual's box (a measured overlay sticks out 5 px at the top) and it is clearly
# smaller than the visual it labels.
_INSIDE = 0.6
_SMALLER = 0.6


@dataclass(frozen=True)
class CaptionTarget:
    """Where a visual's caption lives.

    ``path`` runs from the visual.json root to the property that holds the text (the
    ``paragraphs`` list for a text box). When ``exists`` is false nothing is stored there
    yet: the path names the slot a writer creates, using ``selector`` for the new entry
    (a card label is scoped to its Data field). ``shown`` is false while the caption is
    switched off, so writing text alone would not make it appear.
    """

    where: Where
    path: JsonPath
    exists: bool = False
    shown: bool = True
    selector: dict[str, Any] | None = None


@dataclass(frozen=True)
class _Found:
    target: CaptionTarget
    text: str | None


# --- reading helpers --------------------------------------------------------------------


def _body(visual_json: Mapping[str, Any]) -> dict[str, Any]:
    body = visual_json.get("visual")
    return body if isinstance(body, dict) else {}


def _entries(holder: Any, name: str) -> list[tuple[int, dict[str, Any]]]:
    """(index, entry) of each object entry, keeping the index the entry has in the file."""
    entries = holder.get(name) if isinstance(holder, dict) else None
    if not isinstance(entries, list):
        return []
    return [(i, e) for i, e in enumerate(entries) if isinstance(e, dict)]


def _properties(entry: dict[str, Any]) -> dict[str, Any]:
    props = entry.get("properties")
    return props if isinstance(props, dict) else {}


def _text(node: Any) -> str | None:
    """The text of a string literal, DYNAMIC_TITLE for an expression, None when empty."""
    if node is None:
        return None
    value = expr.decode(node)
    if isinstance(value, str):
        return value if value.strip() else None
    inner = node.get("expr") if isinstance(node, dict) else None
    if isinstance(inner, dict) and value is None and "Literal" not in inner:
        return DYNAMIC_TITLE
    return None


def _flag(node: Any) -> bool | None:
    value = expr.decode(node)
    return value if isinstance(value, bool) else None


def _is_default_scope(entry: dict[str, Any]) -> bool:
    selector = entry.get("selector")
    return not selector or (isinstance(selector, dict) and selector.get("id") == "default")


def _container(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    # A legacy report keeps container formatting under vcObjects.
    for key in ("visualContainerObjects", "vcObjects"):
        value = body.get(key)
        if isinstance(value, dict):
            return key, value
    return "visualContainerObjects", {}


def _last_text(entries: list[tuple[int, dict[str, Any]]]) -> tuple[bool, str | None, int | None]:
    """(shown, text, index of the entry that supplied the text) over entries of one object.

    Properties merge across entries and the last one wins, as in Desktop.
    """
    shown, text, index = True, None, None
    for i, entry in entries:
        props = _properties(entry)
        flag = _flag(props.get("show"))
        if flag is not None:
            shown = flag
        found = _text(props.get("text"))
        if found is not None:
            text, index = found, i
    return shown, text, index


def _projections(body: dict[str, Any]) -> list[tuple[str, int, dict[str, Any]]]:
    query = body.get("query")
    state = query.get("queryState") if isinstance(query, dict) else None
    found: list[tuple[str, int, dict[str, Any]]] = []
    if not isinstance(state, dict):
        return found
    for role, block in state.items():
        projections = block.get("projections") if isinstance(block, dict) else None
        if not isinstance(projections, list):
            continue
        for i, item in enumerate(projections):
            if isinstance(item, dict):
                found.append((str(role), i, item))
    return found


def _shown_name(projection: dict[str, Any]) -> str | None:
    for key in ("displayName", "nativeQueryRef"):
        value = projection.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


# --- per-type locators --------------------------------------------------------------------


def _title(body: dict[str, Any]) -> _Found:
    key, objects = _container(body)
    entries = _entries(objects, "title")
    shown, text, index = _last_text(entries)
    slot = index if index is not None else (entries[-1][0] if entries else 0)
    return _Found(
        CaptionTarget(
            "title",
            ("visual", key, "title", slot, "properties", "text"),
            exists=index is not None,
            shown=shown,
        ),
        text,
    )


def _label(body: dict[str, Any]) -> _Found:
    """A new card's caption: its label, scoped to the first Data field."""
    projections = _projections(body)
    head = next((p for p in projections if p[0] == "Data"), projections[0] if projections else None)
    query_ref = head[2].get("queryRef") if head else None
    selector = {"metadata": query_ref} if isinstance(query_ref, str) else None
    entries = _entries(body.get("objects"), "label")
    # The label of the Data field first, then an unscoped one, then any other entry.
    chosen = next(
        (
            i
            for i, e in entries
            if selector
            and isinstance(e.get("selector"), dict)
            and e["selector"].get("metadata") == query_ref
            and _text(_properties(e).get("text")) is not None
        ),
        None,
    )
    if chosen is None:
        chosen = next(
            (
                i
                for i, e in entries
                if _is_default_scope(e) and _text(_properties(e).get("text")) is not None
            ),
            None,
        )
    if chosen is None:
        chosen = next(
            (i for i, e in entries if _text(_properties(e).get("text")) is not None), None
        )
    text = None
    shown = True
    if chosen is not None:
        entry = dict(entries)[chosen]
        text = _text(_properties(entry).get("text"))
        own = _flag(_properties(entry).get("show"))
        if own is None:
            defaults = [e for _, e in entries if _is_default_scope(e)]
            flags = [
                f for f in (_flag(_properties(e).get("show")) for e in defaults) if f is not None
            ]
            own = flags[-1] if flags else None
        shown = own is not False
    fresh = entries[-1][0] + 1 if entries else 0
    slot = fresh if chosen is None else chosen
    return _Found(
        CaptionTarget(
            "label",
            ("visual", "objects", "label", slot, "properties", "text"),
            exists=chosen is not None,
            shown=shown,
            selector=selector,
        ),
        text,
    )


def _header(body: dict[str, Any]) -> _Found:
    entries = _entries(body.get("objects"), "header")
    shown, text, index = _last_text(entries)
    slot = index if index is not None else (entries[-1][0] if entries else 0)
    return _Found(
        CaptionTarget(
            "header",
            ("visual", "objects", "header", slot, "properties", "text"),
            exists=index is not None,
            shown=shown,
        ),
        text,
    )


def _paragraphs(body: dict[str, Any]) -> _Found:
    entries = _entries(body.get("objects"), "general")
    slot: int | None = None
    text: str | None = None
    for i, entry in entries:
        paragraphs = _properties(entry).get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        slot = i
        lines = [
            "".join(
                str(run.get("value", ""))
                for run in paragraph.get("textRuns", [])
                if isinstance(run, dict)
            )
            for paragraph in paragraphs
            if isinstance(paragraph, dict) and isinstance(paragraph.get("textRuns"), list)
        ]
        joined = " ".join(" ".join(lines).split())[:_TEXT_LIMIT]
        text = joined or None
    return _Found(
        CaptionTarget(
            "paragraphs",
            (
                "visual",
                "objects",
                "general",
                slot if slot is not None else 0,
                "properties",
                "paragraphs",
            ),
            exists=slot is not None,
        ),
        text,
    )


def _button_text(body: dict[str, Any]) -> _Found:
    """An action button's words: ``objects.text`` in its default state (the entry scoped to
    ``{"id": "default"}``, or one without a selector, where Desktop writes the switch). The
    hover, press and disabled states' words are not its caption. Unset, the switch is on
    when there are words, as the canvas draws it."""
    entries = _entries(body.get("objects"), "text")
    shown, text, index = True, None, None
    for i, entry in entries:
        if not _is_default_scope(entry):
            continue
        props = _properties(entry)
        flag = _flag(props.get("show"))
        if flag is not None:
            shown = flag
        found = _text(props.get("text"))
        if found is not None:
            text, index = found, i
    slot = index if index is not None else (entries[-1][0] + 1 if entries else 0)
    return _Found(
        CaptionTarget(
            "button_text",
            ("visual", "objects", "text", slot, "properties", "text"),
            exists=index is not None,
            shown=shown,
            selector={"id": "default"},
        ),
        text,
    )


def _legacy_card(body: dict[str, Any]) -> _Found:
    """A legacy card labels its value with the field's display name."""
    title = _title(body)
    if title.text and title.target.shown:
        return title
    projections = _projections(body)
    if not projections:
        return title
    role, index, projection = projections[0]
    name = _shown_name(projection)
    labels = _entries(body.get("objects"), "categoryLabels")
    hidden = any(_flag(_properties(e).get("show")) is False for _, e in labels)
    if name is None or hidden:
        return title
    return _Found(
        CaptionTarget(
            "projection",
            ("visual", "query", "queryState", role, "projections", index, "displayName"),
            exists=isinstance(projection.get("displayName"), str),
        ),
        name.strip(),
    )


def _locate(visual_json: Mapping[str, Any]) -> _Found:
    body = _body(visual_json)
    kind = body.get("visualType")
    if kind == "textbox":
        return _paragraphs(body)
    if kind == "cardVisual":
        found = _label(body)
        if found.text and found.target.shown:
            return found
        title = _title(body)
        # A card whose label is off shows its container title, so that is what to rename.
        return title if title.text and title.target.shown else found
    if kind == "slicer":
        found = _header(body)
        if found.text and found.target.shown:
            return found
        title = _title(body)
        return title if title.text and title.target.shown else found
    if kind == "card":
        return _legacy_card(body)
    if kind == "actionButton":
        found = _button_text(body)
        if found.text and found.target.shown:
            return found
        # A button without words (or with them switched off) is known by its container title,
        # which is also where a new caption for it goes.
        return _title(body)
    return _title(body)


# --- public API ----------------------------------------------------------------------------


def visible_caption(visual_json: Mapping[str, Any], *, strip: bool = True) -> str | None:
    """The text a user sees for a visual, or None when it shows no caption of its own.

    A card shows its label (else its container title), a slicer its header, a text box its
    text, a legacy card its field's display name, a button its words (else its container
    title), and everything else its container title.
    String literals are decoded (``'Agent''s Sales'`` is ``Agent's Sales``); a caption
    bound to a field or condition is ``DYNAMIC_TITLE``. ``strip=False`` keeps the stored
    spelling ("Agents Avg MTD Sales " with its trailing space).
    """
    found = _locate(visual_json)
    if not found.target.shown or found.text is None:
        return None
    return found.text.strip() if strip else found.text


def caption_target(visual_json: Mapping[str, Any]) -> CaptionTarget:
    """Where the caption shown by ``visible_caption`` is stored (or would be created)."""
    return _locate(visual_json).target


def column_captions(visual_json: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(queryRef, header text) of each field a table or matrix shows, in projection order.

    A column header is the projection's display name (else the field's own name), so
    "rename the MTD Sales column" targets ``displayName`` of one projection.
    """
    found = []
    for _, _, projection in _projections(_body(visual_json)):
        name = _shown_name(projection)
        query_ref = projection.get("queryRef")
        if name is not None and isinstance(query_ref, str):
            found.append((query_ref, name.strip()))
    return found


# --- caption overlays -----------------------------------------------------------------------


@dataclass(frozen=True)
class _Box:
    id: str
    x: float
    y: float
    width: float
    height: float
    z: float
    hidden: bool
    raw: Mapping[str, Any]

    @property
    def area(self) -> float:
        return max(0.0, self.width) * max(0.0, self.height)

    @property
    def kind(self) -> str:
        return str(_body(self.raw).get("visualType", ""))

    @property
    def has_data(self) -> bool:
        return bool(_projections(_body(self.raw)))

    def inside_ratio(self, host: _Box) -> float:
        wide = min(self.x + self.width, host.x + host.width) - max(self.x, host.x)
        high = min(self.y + self.height, host.y + host.height) - max(self.y, host.y)
        if wide <= 0 or high <= 0 or self.area <= 0:
            return 0.0
        return (wide * high) / self.area


def _number(value: Any, default: float = 0.0) -> float:
    return (
        float(value) if isinstance(value, int | float) and not isinstance(value, bool) else default
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _box(item: Any) -> _Box | None:
    if isinstance(item, Mapping):
        raw: Mapping[str, Any] = item
        position = _dict(raw.get("position"))
        name = raw.get("name")
        hidden = raw.get("isHidden") is True
        z = _number(position.get("z"))
        x, y = _number(position.get("x")), _number(position.get("y"))
        width, height = _number(position.get("width")), _number(position.get("height"))
    else:
        raw = _dict(getattr(item, "definition", None))
        position = _dict(raw.get("position"))
        name = getattr(item, "id", None)
        hidden = bool(getattr(item, "hidden", raw.get("isHidden") is True))
        z = _number(getattr(item, "z", position.get("z")))
        x, y = _number(getattr(item, "x", 0)), _number(getattr(item, "y", 0))
        width, height = _number(getattr(item, "width", 0)), _number(getattr(item, "height", 0))
    if not isinstance(name, str) or ("visualGroup" in raw and "visual" not in raw):
        return None
    return _Box(name, x, y, width, height, z, hidden, raw)


def _link(item: Any) -> tuple[str, bool, str | None] | None:
    """(name, own hidden flag, parent group name) of a visual or group item."""
    if isinstance(item, Mapping):
        name, hidden, parent = (
            item.get("name"),
            item.get("isHidden") is True,
            item.get("parentGroupName"),
        )
    else:
        raw = _dict(getattr(item, "definition", None))
        name = getattr(item, "id", None)
        hidden = bool(getattr(item, "hidden", raw.get("isHidden") is True))
        parent = getattr(item, "parent_group", raw.get("parentGroupName"))
    if not isinstance(name, str):
        return None
    return name, hidden, parent if isinstance(parent, str) else None


def _not_drawn(items: list[Any]) -> set[str]:
    """Names of visuals and groups Desktop does not draw: hidden themselves, or inside a
    hidden group (a group's flag reaches its members and nested groups)."""
    links = {link[0]: link[1:] for link in map(_link, items) if link is not None}
    hidden: set[str] = set()
    for name in links:
        seen: set[str] = set()
        current: str | None = name
        while current is not None and current in links and current not in seen:
            seen.add(current)
            own, parent = links[current]
            if own:
                hidden.add(name)
                break
            current = parent
    return hidden


def _text_only(box: _Box) -> bool:
    """A visual that only draws words: a text box, or a card whose value is switched off
    (its container title is then all it shows, as on ``8a8ba163``)."""
    if visible_caption(box.raw) is None:
        return False
    if box.kind == "textbox":
        return True
    if box.kind == "cardVisual":
        entries = _entries(_body(box.raw).get("objects"), "value")
        return any(_flag(_properties(e).get("show")) is False for _, e in entries)
    return False


def find_caption_overlays(page_visuals: Iterable[Any]) -> list[tuple[str, str]]:
    """(overlay id, host id) pairs on one page: a text-only visual lying over a data visual.

    Authors label a slicer or chart by dropping a text box (or an emptied card) on top of
    it, so the words a user reads belong to the visual underneath. A visual can lie over
    several hosts; every pair is returned, smallest host first, so chat can offer both
    targets. Items are ImportedVisual objects or raw visual.json dicts; hidden visuals, and
    visuals inside a hidden group, never take part.
    """
    items = list(page_visuals)
    not_drawn = _not_drawn(items)
    boxes = [
        b
        for b in (_box(item) for item in items)
        if b is not None and not b.hidden and b.id not in not_drawn
    ]
    pairs: list[tuple[str, str]] = []
    for overlay in boxes:
        if not _text_only(overlay) or overlay.area <= 0:
            continue
        hosts = [
            host
            for host in boxes
            if host.id != overlay.id
            and host.has_data
            and not _text_only(host)
            and host.z < overlay.z
            and overlay.inside_ratio(host) >= _INSIDE
            and overlay.area <= host.area * _SMALLER
        ]
        pairs.extend((overlay.id, host.id) for host in sorted(hosts, key=lambda h: h.area))
    return pairs
