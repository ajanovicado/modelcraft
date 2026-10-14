"""Reading formatting properties the way Power BI resolves them.

Three things live here because every renderer token needs them: literal decoding (strings,
``16D`` doubles, ``45L`` integers, ``1.5M`` decimals, ``datetime'..'``), colors (literals and
ThemeDataColor with Power BI's shading), and selector specificity. An object such as
``dataPoint`` or ``value`` holds a list of entries; each has an optional selector. Desktop
does not apply them in list order: a selector-less or ``{"id":"default"}`` entry sets the
base, a ``metadata`` entry (one field) overrides it, a ``data`` entry (one category or one
series) overrides that, and a state entry (``selection:selected``) beats them all. Two entries
of equal specificity resolve to the later one.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import pbir_expr
from .report_theme import DEFAULT_THEME, theme_palette

Json = dict[str, Any]

_NAMED = {
    "black": "#000000",
    "white": "#ffffff",
    "red": "#ff0000",
    "green": "#008000",
    "blue": "#0000ff",
    "gray": "#808080",
    "grey": "#808080",
}
_HEX = re.compile(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})")


def value(node: Any, default: Any = None) -> Any:
    """The Python value of a literal property expression, ``default`` for anything else.

    D and L numbers both come back as numbers, M (fixed decimal) as a float and
    ``datetime'..'`` as a datetime. Color nodes and measure expressions give ``default``.
    """
    if not isinstance(node, dict) or "solid" in node:
        return default
    decoded = pbir_expr.decode(node)
    return default if decoded is None else decoded


def prop(node: Any, default: Any = None) -> Any:
    """``value`` that also accepts the plain scalars a theme file uses."""
    if isinstance(node, bool | int | float | str):
        return node
    return value(node, default)


def is_dynamic(node: Any) -> bool:
    """True for an expression that is computed from data (a measure, a rule, a field)."""
    if not isinstance(node, dict):
        return False
    inner: Any = node
    if "solid" in node:
        solid = node["solid"]
        inner = solid.get("color") if isinstance(solid, dict) else None
    expression = inner.get("expr") if isinstance(inner, dict) else None
    if not isinstance(expression, dict) or not expression:
        return False
    return next(iter(expression)) not in {"Literal", "ThemeDataColor"}


def number(node: Any, default: float | None = None) -> float | None:
    found = prop(node)
    if isinstance(found, bool) or not isinstance(found, int | float) or not math.isfinite(found):
        return default
    return float(found)


def flag(node: Any, default: bool | None = None) -> bool | None:
    found = prop(node)
    return found if isinstance(found, bool) else default


MAX_TEXT = 2000


def text(node: Any, default: str | None = None) -> str | None:
    """A text property, cut to 2,000 characters. It is text for ``textContent``, not markup."""
    found = prop(node)
    return found[:MAX_TEXT] if isinstance(found, str) else default


_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _,.-]{0,59}")


def enum_text(node: Any, default: str | None = None) -> str | None:
    """A short label-like property (a position, a shape name, a mode): letters, digits and a
    few separators only, else ``default``."""
    found = prop(node)
    return found if isinstance(found, str) and _WORD.fullmatch(found) else default


def px(points: float | None) -> float | None:
    """CSS pixels for a point size (Power BI sizes are points; a CSS px is 3/4 pt)."""
    if points is None:
        return None
    return round(max(1.0, min(points, 300.0)) * 4 / 3, 2)


# --- colors -----------------------------------------------------------------------------


def safe_color(color: Any) -> str | None:
    """A color that is safe to hand to CSS: a name from a short list or a 3/6/8-digit hex."""
    if isinstance(color, str) and color.lower() in _NAMED | {"transparent": ""}:
        return color.lower()
    return color if isinstance(color, str) and _HEX.fullmatch(color) else None


def palette_of(theme: Any) -> list[str]:
    """ColorId palette of a theme dict, or a palette list passed as is."""
    if isinstance(theme, Mapping):
        palette = theme.get("palette")
        if isinstance(palette, list):
            return [c if isinstance(c, str) else "" for c in palette]
        return theme_palette(theme.get("dataColors", DEFAULT_THEME["dataColors"]))
    if isinstance(theme, Sequence) and not isinstance(theme, str):
        return [c if isinstance(c, str) else "" for c in theme]
    return theme_palette(DEFAULT_THEME["dataColors"])


def shade(base: str, percent: float) -> str:
    """Power BI's ThemeDataColor shading, with channels rounded half up (floor(x + 0.5)).

    Python's round() goes to the even neighbor: 178.5 gives 178 where Power BI gives 179.
    """
    p = max(-1.0, min(1.0, float(percent)))
    channels = [int(base[i : i + 2], 16) for i in (1, 3, 5)]
    if p < 0:
        shaded = [math.floor(c * (1 + p) + 0.5) for c in channels]
    else:
        shaded = [math.floor(c + (255 - c) * p + 0.5) for c in channels]
    return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in shaded)


def color(node: Any, theme: Any) -> str | None:
    """A CSS color for a ``{solid: {color}}`` property (or a bare color expression), else None.

    ``theme`` is a theme dict or a ColorId palette. A ThemeDataColor id outside the palette
    is invalid in Desktop (it draws the default fill), so it gives None here too.
    """
    if not isinstance(node, dict):
        return None
    inner: Any = node.get("solid", {}).get("color", node) if "solid" in node else node
    if isinstance(inner, str):
        return safe_color(inner)
    if not isinstance(inner, dict):
        return None
    direct = safe_color(value(inner))
    if direct:
        return direct
    themed = inner.get("expr", {}).get("ThemeDataColor", {})
    index = themed.get("ColorId") if isinstance(themed, dict) else None
    palette = palette_of(theme)
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(palette):
        return None
    base = palette[index]
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", base):
        return None
    percent = themed.get("Percent", 0)
    return shade(base, float(percent) if isinstance(percent, int | float) else 0.0)


def themed_color_problem(node: Any, theme: Any) -> bool:
    """True when a color expression names a ThemeDataColor Desktop cannot resolve (id 10+)."""
    if not isinstance(node, dict):
        return False
    inner: Any = node.get("solid", {}).get("color", node) if "solid" in node else node
    themed = inner.get("expr", {}).get("ThemeDataColor") if isinstance(inner, dict) else None
    return isinstance(themed, dict) and color(node, theme) is None


def expand_hex(color_text: str) -> str | None:
    """``#rrggbb`` for a 3- or 6-digit hex or a named color; None for 8 digits or CSS words."""
    lowered = color_text.lower()
    if lowered in _NAMED:
        return _NAMED[lowered]
    if re.fullmatch(r"#[0-9a-f]{3}", lowered):
        return "#" + "".join(ch * 2 for ch in lowered[1:])
    return lowered if re.fullmatch(r"#[0-9a-f]{6}", lowered) else None


def with_alpha(color_text: str | None, transparency: float | None) -> str | None:
    """``#rrggbbaa`` for a color and Power BI's transparency (percent, 0 opaque, 100 clear)."""
    if color_text is None:
        return None
    base = expand_hex(color_text)
    if base is None:
        return color_text if _HEX.fullmatch(color_text) or color_text == "transparent" else None
    percent = 0.0 if transparency is None else max(0.0, min(100.0, transparency))
    alpha = math.floor((1 - percent / 100) * 255 + 0.5)
    return f"{base}{alpha:02x}"


def alpha_of(transparency: float | None) -> float:
    """Opacity 0..1 for a transparency percent."""
    percent = 0.0 if transparency is None else max(0.0, min(100.0, transparency))
    return round(1 - percent / 100, 4)


# --- selector specificity ---------------------------------------------------------------


# A table's rule-driven font color is scoped to "every value cell of this column" with a
# dataViewWildcard selector, which only covers an instance that is a data point.
WILDCARD_ROW: Json = {"dataViewWildcard": {"matchingOption": 1}}


def data_target(*values: Any) -> list[str]:
    """Selector data for category values, in the literal spelling entries carry.

    2019 and 2019.0 are the same instance; a date is ``datetime'..'``.
    """
    found: list[str] = []
    for item in values:
        if isinstance(item, datetime):
            found.append(f"datetime'{item.strftime('%Y-%m-%dT%H:%M:%S')}'")
        elif isinstance(item, float) and item.is_integer():
            found.append(f"{int(item)}L")
        else:
            found.append(pbir_expr.literal_value(item))
    return found


def _entries(objects: Any, name: str) -> list[Json]:
    found = objects.get(name) if isinstance(objects, dict) else None
    # An entry whose selector is not an object is malformed: Desktop drops it, so do we.
    return (
        [e for e in found if isinstance(e, dict) and isinstance(e.get("selector") or {}, dict)]
        if isinstance(found, list)
        else []
    )


def covering(
    objects: Any,
    name: str,
    metadata: str | None = None,
    identity: str | None = None,
    data: Sequence[Any] | None = None,
    *,
    selector_required: bool = False,
) -> list[Json]:
    """Entries of object ``name`` that cover one instance, weakest specificity first.

    ``metadata`` is the field's queryRef, ``identity`` a state id (``selection:selected``, a
    reference label's ``field-<uuid>``), ``data`` the category or series values of a data
    point (see ``data_target``). ``selector_required`` drops selector-less entries: a shape
    only honors formatting written under a state selector such as ``{"id": "default"}``.
    """
    target: Json = {}
    if metadata:
        target["metadata"] = metadata
    if identity:
        target["state"] = identity
    if data:
        target["data"] = list(data)
    entries = _entries(objects, name)
    if not entries:
        return []
    scoped = {name: entries}
    found = [c for c in pbir_expr.entries_covering(scoped, name, target)]
    if selector_required:
        found = [c for c in found if c.entry.get("selector")]
    return [c.entry for c in reversed(found)]


def properties(
    objects: Any,
    name: str,
    metadata: str | None = None,
    identity: str | None = None,
    data: Sequence[Any] | None = None,
    *,
    selector_required: bool = False,
) -> Json:
    """The properties of object ``name`` for one instance, resolved by specificity.

    A property set by a narrower entry wins over the same property in a broader one, whatever
    their order in the file: ``[{metadata: F, size: 20}, {id: default, size: 10}]`` gives 20
    for field F.
    """
    merged: Json = {}
    for entry in covering(
        objects, name, metadata, identity, data, selector_required=selector_required
    ):
        props = entry.get("properties")
        if isinstance(props, dict):
            merged.update(props)
    return merged


def theme_defaults(theme: Mapping[str, Any], visual_type: str, name: str) -> Json:
    """The theme's own defaults for one object: ``visualStyles['*']['*'][name]`` then the
    visual type's, later winning per property. State entries (``$id``) are not defaults."""
    styles = theme.get("visualStyles")
    merged: Json = {}
    if not isinstance(styles, dict):
        return merged
    for key in ("*", visual_type):
        body = styles.get(key)
        inner = body.get("*") if isinstance(body, dict) else None
        entries = inner.get(name) if isinstance(inner, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("$id", "default") == "default":
                merged.update({k: v for k, v in entry.items() if k != "$id"})
    return merged
