"""PBIR property expressions, selectors and the formatting catalog, as Desktop writes them.

Every formatting value in a visual.json is an expression. The Agents Performance report
(Desktop 2.157) uses these literal shapes:
- strings in quotes, where a quote inside is doubled ('O''Neil');
- doubles with a D suffix ('16D') and integers with an L suffix ('45L'); model data
  literals also use M for fixed decimals ('19.5M') and datetime'2019-01-01T00:00:00';
- true/false.

The catalog decides which number shape a property takes, with two corrections. A
"formatting" property has a subtype (fontSize and labelDisplayUnits are numbers,
fontFamily, alignment, formatString and altText are text), and Desktop's own choice wins
over the catalog: accentBar.width is "numeric" in the catalog but Desktop wrote 6L, so
an overwrite reuses the suffix of the literal already there and reading treats D and L
alike. Colors are {solid: {color: {expr}}}, where the expression is either a literal hex
or ThemeDataColor{ColorId, Percent}. A live Desktop probe measured the ids: 0 is always
#FFFFFF and 1 always #000000 whatever the theme's background and foreground are, 2..9
are dataColors[0..7], and ids of 10 and up are invalid (Desktop draws the default fill).
The "Others" bars (ColorId 0, Percent -0.2) render #CCCCCC, which is white shaded 20%
by Power BI's shadeColor.

Some visual types (`shape`, `cardVisual`, buttons, tiles) only honor an object that sits
under a state selector such as {"id": "default"}: a `shape` fill written without it drew
in the theme's first color. ``instance_selectors`` and ``needs_default_selector`` say
which type/object pairs, and the writers (``set_property``, ``write_everywhere``) take a
``visual_type`` to apply the rule.

Microsoft's expr codec does not escape quotes, so Studio owns the escaping.
"""

from __future__ import annotations

import copy
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from importlib.resources import files
from typing import Any, NamedTuple

_HEX = re.compile(r"#?([0-9A-Fa-f]{6})")


class ExpressionError(ValueError):
    """A value cannot be written as this property's PBIR expression."""


# --- literals -----------------------------------------------------------------------


def _literal(value: str) -> dict[str, Any]:
    return {"expr": {"Literal": {"Value": value}}}


def quote(text: str) -> str:
    """A PBIR string literal: quotes doubled inside, as Desktop writes 'O''Neil'."""
    if not isinstance(text, str):
        raise ExpressionError("Text values must be strings")
    if any(ord(c) < 32 and c not in "\t\n\r" for c in text):
        raise ExpressionError("Text values cannot contain control characters")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as error:
        # A lone surrogate (half of an emoji) cannot be saved in the report file.
        raise ExpressionError("Text values cannot contain unpaired surrogate characters") from error
    return "'" + text.replace("'", "''") + "'"


def string(text: str) -> dict[str, Any]:
    return _literal(quote(text))


def boolean(flag: bool) -> dict[str, Any]:
    if not isinstance(flag, bool):
        raise ExpressionError("Expected true or false")
    return _literal("true" if flag else "false")


def _finite(number: float | int) -> float | int:
    if isinstance(number, bool) or not isinstance(number, int | float):
        raise ExpressionError("Expected a finite number")
    try:
        finite = math.isfinite(number)
    except OverflowError:  # an int too big for a float
        finite = False
    if not finite:
        raise ExpressionError("Expected a finite number")
    return number


# Desktop reads a whole number as a signed 64-bit value, and a double this large is written
# with hundreds of digits by the plain spelling: neither is a value anyone means to type.
_INT64_LIMIT = 2**63
_DOUBLE_LIMIT = 1e15


def _long(number: float | int) -> int:
    """A whole number that fits Desktop's 64-bit integer literal."""
    whole = int(number)
    if not -_INT64_LIMIT <= whole < _INT64_LIMIT:
        raise ExpressionError(f"{whole} is too large to be a whole number in Power BI")
    return whole


def _sane(number: float | int) -> float | int:
    if abs(number) > _DOUBLE_LIMIT:
        raise ExpressionError(
            f"{number:g} is out of range: numbers in Power BI formatting stay within "
            f"{_DOUBLE_LIMIT:,.0f}"
        )
    return number


def double(number: float | int) -> dict[str, Any]:
    number = _sane(_finite(number))
    text = str(int(number)) if float(number).is_integer() else repr(float(number))
    return _literal(f"{text}D")


def integer(number: float | int) -> dict[str, Any]:
    number = _finite(number)
    if not float(number).is_integer():
        raise ExpressionError("Expected a whole number")
    return _literal(f"{_long(number)}L")


def hex_color(color: str) -> str:
    match = _HEX.fullmatch(color.strip()) if isinstance(color, str) else None
    if not match:
        raise ExpressionError(f"Expected a hex color like #1E90FF, not {color!r}")
    return "#" + match.group(1).upper()


def color(value: str) -> dict[str, Any]:
    return {"solid": {"color": _literal(quote(hex_color(value)))}}


# The ThemeDataColor ids Desktop honors, measured by a live probe under a custom theme:
# 0 is white, 1 is black (the "Others" bars, ColorId 0 at -0.2, render #CCCCCC) and 2..9
# are the first eight data colors. Ids of 10 and up are invalid: Desktop ignores the
# fill and draws the default one, so they must never be written.
VERIFIED_THEME_COLOR_IDS = frozenset(range(10))


def theme_color(
    color_id: int, percent: float = 0, *, verified_only: bool = False
) -> dict[str, Any]:
    """A theme color expression for ids 0..9.

    ``verified_only`` is kept for callers that pass it; every id that can be written is
    verified now, so it changes nothing.
    """
    if (
        isinstance(color_id, bool)
        or not isinstance(color_id, int)
        or color_id not in VERIFIED_THEME_COLOR_IDS
    ):
        raise ExpressionError(
            "Theme colors are numbered 0 to 9 (white, black, then the first eight "
            "theme colors); use a hex color for anything else"
        )
    if not -1 <= float(percent) <= 1:
        raise ExpressionError("Theme color shading runs from -1 to 1")
    themed = {"ThemeDataColor": {"ColorId": color_id, "Percent": percent}}
    return {"solid": {"color": {"expr": themed}}}


def literal_value(value: Any) -> str:
    """The Literal text for a data value used in a filter or category selector."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{_long(value)}L"
    if isinstance(value, float):
        _finite(value)
        return f"{repr(value)}D"
    if isinstance(value, datetime):
        return f"datetime'{value.strftime('%Y-%m-%dT%H:%M:%S')}'"
    if isinstance(value, date):
        return f"datetime'{value.isoformat()}T00:00:00'"
    if isinstance(value, str):
        return quote(value)
    raise ExpressionError(f"Unsupported value {value!r}")


# Analysis Services DataType ids as they appear in the model's Column/Measure tables.
_MODEL_TYPES = {2: "text", 6: "integer", 8: "double", 9: "datetime", 10: "decimal", 11: "boolean"}
DATA_TYPES = frozenset(_MODEL_TYPES.values()) | {"date"}


def data_type_name(data_type: str | int | None) -> str | None:
    """The type name for a model DataType id (2 text, 6 whole, 8 float, 9 date/time,
    10 fixed decimal, 11 true/false) or a name already in ``DATA_TYPES``."""
    if isinstance(data_type, bool):
        return None
    if isinstance(data_type, int):
        return _MODEL_TYPES.get(data_type)
    if isinstance(data_type, str):
        name = data_type.strip().casefold()
        return name if name in DATA_TYPES else None
    return None


def data_literal(value: Any, data_type: str | int) -> str:
    """The Literal text for ``value`` as a column or measure of ``data_type`` stores it.

    Desktop types a data literal by the field, not by the Python type of the value: 2019 on
    an integer axis is ``2019L``, on a text axis ``'2019'``, on a fixed-decimal column
    ``2019M``. A value that cannot be read as the type raises ExpressionError, so chat can
    ask instead of writing a filter or category color that never matches.
    """
    name = data_type_name(data_type)
    if name is None:
        raise ExpressionError(f"Unknown data type {data_type!r}")
    if name == "text":
        if isinstance(value, bool) or not isinstance(value, str | int | float):
            raise ExpressionError(f"{value!r} is not text")
        return quote(str(value))
    if name == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        lowered = str(value).strip().casefold()
        if lowered in {"true", "yes", "1"}:
            return "true"
        if lowered in {"false", "no", "0"}:
            return "false"
        raise ExpressionError(f"{value!r} is not true or false")
    if name in {"datetime", "date"}:
        if isinstance(value, datetime):
            when = value
        elif isinstance(value, date):
            when = datetime(value.year, value.month, value.day)
        elif isinstance(value, str):
            try:
                when = datetime.fromisoformat(value.strip())
            except ValueError as error:
                raise ExpressionError(f"{value!r} is not a date (use YYYY-MM-DD)") from error
        else:
            raise ExpressionError(f"{value!r} is not a date")
        return f"datetime'{when.strftime('%Y-%m-%dT%H:%M:%S')}'"
    number = _number(value)
    if name == "integer":
        if not float(number).is_integer():
            raise ExpressionError(f"{value!r} is not a whole number")
        return f"{_long(number)}L"
    if name == "decimal":
        return f"{_plain(number)}M"
    return f"{_plain(number)}D"


def _number(value: Any) -> float | int:
    if isinstance(value, bool):
        raise ExpressionError(f"{value!r} is not a number")
    if isinstance(value, int | float):
        return _finite(value)
    if isinstance(value, str):
        text = value.strip()
        # Commas only as thousands separators: "1,5" (a European decimal) or a typo
        # would otherwise be written silently as 15.
        if "," in text:
            if not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", text):
                raise ExpressionError(f"{value!r} is not a number")
            text = text.replace(",", "")
        try:
            return _finite(int(text) if re.fullmatch(r"-?\d+", text) else float(text))
        except ValueError as error:
            raise ExpressionError(f"{value!r} is not a number") from error
    raise ExpressionError(f"{value!r} is not a number")


def _plain(number: float | int) -> str:
    return str(int(number)) if float(number).is_integer() else repr(float(number))


_NUMBER = re.compile(r"(-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)([DLM])")
_PLAIN_INT = re.compile(r"-?\d+")


def _parse_number(digits: str, suffix: str) -> float | int:
    """An L literal with plain digits as an exact int, everything else as a float."""
    if suffix == "L" and _PLAIN_INT.fullmatch(digits):
        return int(digits)
    return float(digits)


_DATETIME = re.compile(r"datetime'(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?)?)'")


def _literal_text(node: Any) -> str | None:
    """The Literal text of an expression node (``{expr: {Literal}}`` or ``{Literal}``)."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return None
    inner = node.get("expr", node)
    literal = inner.get("Literal") if isinstance(inner, dict) else None
    value = literal.get("Value") if isinstance(literal, dict) else None
    return value if isinstance(value, str) else None


def number_suffix(node: Any) -> str | None:
    """``D``, ``L`` or ``M`` for a numeric literal expression (or its Literal text), else None."""
    match = _NUMBER.fullmatch(_literal_text(node) or "")
    return match.group(2) if match else None


def decode(node: Any) -> Any:
    """A Python value for a literal expression, a color dict for colors, else None.

    D and L numbers are interchangeable when reading: Desktop writes ``6L`` where the
    catalog says numeric, so 6D and 6L both come back as the number 6 (L as an int, D as
    a float). M (fixed decimal) reads as a float and ``datetime'...'`` as a datetime.
    Use ``number_suffix`` when the spelling matters.
    """
    if not isinstance(node, dict):
        return None
    if "solid" in node:
        inner = node.get("solid", {}).get("color", {})
        themed = inner.get("expr", {}).get("ThemeDataColor") if isinstance(inner, dict) else None
        if isinstance(themed, dict):
            return {"theme": themed.get("ColorId"), "percent": themed.get("Percent", 0)}
        text = decode(inner)
        return {"hex": text} if isinstance(text, str) else None
    literal = _literal_text(node)
    if literal is None:
        return None
    if len(literal) >= 2 and literal.startswith("'") and literal.endswith("'"):
        return literal[1:-1].replace("''", "'")
    if literal in {"true", "false"}:
        return literal == "true"
    match = _NUMBER.fullmatch(literal)
    if match:
        number = _parse_number(match.group(1), match.group(2))
        return int(number) if match.group(2) == "L" else float(number)
    when = _DATETIME.fullmatch(literal)
    if when:
        try:
            return datetime.fromisoformat(when.group(1))
        except ValueError:
            return None
    return None


def shade(hex_value: str, percent: float) -> str:
    """Power BI's ThemeDataColor shading (powerbi-report-author shadeColor).

    Channels round half up, ``floor(x + 0.5)``. Python's ``round`` goes to the even
    neighbor, so round(178.5) is 178 where Power BI gives 179: white shaded -30% would
    come out #B2B2B2 instead of #B3B3B3.
    """
    base = hex_color(hex_value)[1:]
    channels = [int(base[i : i + 2], 16) for i in (0, 2, 4)]
    if percent > 0:
        channels = [math.floor(c + (255 - c) * percent + 0.5) for c in channels]
    elif percent < 0:
        channels = [math.floor(c * (1 + percent) + 0.5) for c in channels]
    return "#" + "".join(f"{max(0, min(255, c)):02X}" for c in channels)


def theme_palette(theme: dict[str, Any]) -> list[str]:
    """ColorId order: white, black, then the first eight dataColors (measured in Desktop).

    The theme's own background and foreground are not used: Desktop paints ids 0 and 1
    white and black under any theme. A data color that is not a hex stays as an empty
    slot so later ids keep their meaning; resolving it raises.
    """
    palette = ["#FFFFFF", "#000000"]
    colors = theme.get("dataColors", [])
    for item in (colors if isinstance(colors, list) else [])[:8]:
        match = _HEX.fullmatch(item.strip()) if isinstance(item, str) else None
        palette.append("#" + match.group(1).upper() if match else "")
    return palette


def resolve_theme_color(
    theme: dict[str, Any], color_id: int, percent: float = 0, *, verified_only: bool = False
) -> str:
    """The hex a ThemeDataColor{ColorId, Percent} paints under ``theme``."""
    theme_color(color_id, percent, verified_only=verified_only)  # validates id and range
    palette = theme_palette(theme)
    if color_id >= len(palette) or not palette[color_id]:
        raise ExpressionError(f"This theme has no color number {color_id}")
    return shade(palette[color_id], float(percent))


def color_hex(node: Any, palette: list[str] | None = None) -> str | None:
    """The hex a color property paints: a literal hex, or a theme color resolved through
    ``palette`` (``theme_palette``). None for a measure, a rule, a color name or a theme
    color without a palette to resolve it."""
    decoded = decode(node)
    if not isinstance(decoded, dict):
        return None
    if "hex" in decoded:
        match = _HEX.fullmatch(str(decoded["hex"]).strip())
        return "#" + match.group(1).upper() if match else None
    color_id, percent = decoded.get("theme"), decoded.get("percent", 0)
    if (
        palette is None
        or isinstance(color_id, bool)
        or not isinstance(color_id, int)
        or not 0 <= color_id < len(palette)
        or not palette[color_id]
        or not isinstance(percent, int | float)
    ):
        return None
    return shade(palette[color_id], float(percent))


def hue_family(hex_value: str) -> str:
    """A plain color name for a hex color: what a person would call it ("blue", "grey").

    Chat says "make the blue bars green" and the receipt says "replaces TopN blue", so the
    editor and the resolver share one word list. Families: black, white, grey, red, orange,
    brown, yellow, green, teal, blue, purple, pink.
    """
    base = hex_color(hex_value)[1:]
    red, green, blue = (int(base[i : i + 2], 16) / 255 for i in (0, 2, 4))
    top, bottom = max(red, green, blue), min(red, green, blue)
    spread = top - bottom
    if top < 0.16:
        return "black"
    saturation = spread / top
    if saturation < 0.12:
        return "white" if top > 0.9 else "grey"
    if top == red:
        hue = (60 * ((green - blue) / spread)) % 360
    elif top == green:
        hue = 60 * ((blue - red) / spread) + 120
    else:
        hue = 60 * ((red - green) / spread) + 240
    if hue < 15 or hue >= 345:
        return "pink" if saturation < 0.45 and top > 0.75 else "red"
    if hue < 45:
        return "brown" if top < 0.65 else "orange"
    if hue < 70:
        return "yellow"
    if hue < 165:
        return "green"
    if hue < 200:
        return "teal"
    # Power BI's own purple (#744EC2, hue 259.7) and CSS purple (#800080, hue 300) are purple;
    # magenta and orchid (light, same hue band) are pink, so the split above 300 is by lightness.
    if hue < 250:
        return "blue"
    if hue < 300 or (hue < 315 and top < 0.75):
        return "purple"
    return "pink"


# --- fonts ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FontLiteral:
    """A fontFamily Literal text and whether Power BI's font list has that family."""

    text: str
    known: bool
    family: str = ""
    # True only for stacks copied from a file Desktop wrote (the Agents report).
    verified: bool = False


def _stack(name: str, web_font: str) -> str:
    return f"'{name}', {web_font}, helvetica, arial, sans-serif"


# Desktop writes the Segoe UI family as a CSS stack whose first entry is the quoted
# family, so the value is stored quoted again inside the literal:
# '''Segoe UI Bold'', wf_segoe-ui_bold, helvetica, arial, sans-serif'. The three
# verified stacks are the only distinct fontFamily values in the 14 literals of the
# Agents Performance report (read from the file on 2026-09-28).
_VERIFIED_STACKS = {
    "Segoe UI": _stack("Segoe UI", "wf_segoe-ui_normal"),
    "Segoe UI Semibold": _stack("Segoe UI Semibold", "wf_segoe-ui_semibold"),
    "Segoe UI Bold": _stack("Segoe UI Bold", "wf_segoe-ui_bold"),
}
# Power BI's own stack for the light weight follows the same pattern; not yet seen in a
# file Desktop wrote.
_UNVERIFIED_STACKS = {"Segoe UI Light": _stack("Segoe UI Light", "wf_segoe-ui_light")}
# The rest of Power BI's font picker; Desktop stores these as the plain quoted name.
_PLAIN_FAMILIES = (
    "Arial",
    "Arial Black",
    "Arial Unicode MS",
    "Calibri",
    "Cambria",
    "Cambria Math",
    "Candara",
    "Comic Sans MS",
    "Consolas",
    "Constantia",
    "Corbel",
    "Courier New",
    "DIN",
    "Georgia",
    "Lucida Sans Unicode",
    "Symbol",
    "Tahoma",
    "Times New Roman",
    "Trebuchet MS",
    "Verdana",
    "Wingdings",
)
_FONTS = {
    name.casefold(): name for name in (*_VERIFIED_STACKS, *_UNVERIFIED_STACKS, *_PLAIN_FAMILIES)
}


def font_literal(family: str) -> FontLiteral:
    """The fontFamily Literal text for a font name as the Format pane lists it.

    A family Power BI lists as a stack (Segoe UI and its weights) gets the stack Desktop
    writes; other listed families get their plain quoted name. A family not in the table
    also gets the plain quoted name with ``known=False``: Power BI shows it only if it is
    installed on the viewer's machine, so the caller should warn.
    """
    if not isinstance(family, str) or not family.strip():
        raise ExpressionError("Give a font name")
    name = family.strip()
    canonical = _FONTS.get(name.casefold())
    if canonical is None:
        # An already-built stack (from a file Studio read) passes through as known.
        for known, stack in {**_VERIFIED_STACKS, **_UNVERIFIED_STACKS}.items():
            if name == stack:
                canonical = known
                break
    if canonical is None:
        return FontLiteral(quote(name), False, name)
    if canonical in _VERIFIED_STACKS:
        return FontLiteral(quote(_VERIFIED_STACKS[canonical]), True, canonical, True)
    if canonical in _UNVERIFIED_STACKS:
        return FontLiteral(quote(_UNVERIFIED_STACKS[canonical]), True, canonical)
    return FontLiteral(quote(canonical), True, canonical)


# --- selectors and object entries ---------------------------------------------------


def metadata_selector(query_ref: str) -> dict[str, Any]:
    return {"metadata": query_ref}


def column_ref(entity: str, prop: str) -> dict[str, Any]:
    return {"Column": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def measure_ref(entity: str, prop: str) -> dict[str, Any]:
    return {"Measure": {"Expression": {"SourceRef": {"Entity": entity}}, "Property": prop}}


def category_selector(
    entity: str, prop: str, value: Any, data_type: str | int | None = None
) -> dict[str, Any]:
    """One category's data point (a per-bar color), as Desktop scopes it.

    ``data_type`` (a model DataType id or name) types the literal from the field like
    ``data_literal``; without it the literal follows the Python type of ``value``.
    """
    text = literal_value(value) if data_type is None else data_literal(value, data_type)
    return {
        "data": [
            {
                "scopeId": {
                    "Comparison": {
                        "ComparisonKind": 0,
                        "Left": column_ref(entity, prop),
                        "Right": {"Literal": {"Value": text}},
                    }
                }
            }
        ]
    }


# A selector is an open record {data?, metadata?, id?, order?} that Studio keeps verbatim.
# State ids come from what Desktop writes, never from the catalog's instance-selectors
# list, which lacks them: 53 id selectors in the Agents report are not in that list
# ("selection:selected" on slicer fill/value/outline, "field-<uuid>" on the new card's
# reference labels, "default").
STATE_SELECTED = "selection:selected"
STATE_DEFAULT = "default"


def state_selector(state_id: str) -> dict[str, Any]:
    """The selector for a visual state such as a selected slicer tile."""
    return {"id": state_id}


def same_selector(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    # A missing selector and {"id": "default"} both mean "the whole visual" only in
    # container objects; data objects distinguish them, so compare literally.
    return json.dumps(a or None, sort_keys=True) == json.dumps(b or None, sort_keys=True)


_same_selector = same_selector


def selector_kind(selector: dict[str, Any] | None) -> str:
    """none, default, state, metadata, data, data+metadata or field (a reference label).

    A selector with a state id ("selection:selected") is "state" whatever else it holds;
    "field-<uuid>" ids, which come with data and metadata, are "field". Keys Studio does
    not know (order, future ones) never change the answer.
    """
    if not selector:
        return "none"
    selector_id = selector.get("id")
    if isinstance(selector_id, str):
        if selector_id == STATE_DEFAULT:
            return "default"
        return "field" if selector_id.startswith("field-") else "state"
    has_data = bool(selector.get("data"))
    has_metadata = isinstance(selector.get("metadata"), str)
    if has_data and has_metadata:
        return "data+metadata"
    if has_data:
        return "data"
    if has_metadata:
        return "metadata"
    return "none"


def with_article(visual_type: str) -> str:
    """ "A columnChart", "An actionButton": how an error names a visual type."""
    return f"{'An' if visual_type[:1].lower() in 'aeiou' else 'A'} {visual_type}"


def instance_selectors(visual_type: str) -> dict[str, list[str]]:
    """{object: [states]} for the objects of a visual type that need a state selector.

    From the schema package's instance-selectors.json (kept in the catalog snapshot): a
    `shape`'s fill, outline and shape, a button's or tile's fill and text per state, a new
    card's objects under "default". Empty for a type that lists none. The states are what
    the file lists (`default`, `hover`, `selected`, `disabled`; a matrix's `Row` and
    `Column` totals; a filter card's `Available` and `Applied`), not every id Desktop
    writes: `selection:selected` on a slicer tile is not in it.
    """
    found = catalog().get("instanceSelectors", {}).get(visual_type, {})
    listed = {str(name): [str(s) for s in states] for name, states in found.items()}
    for stored, listed_as in _STATE_ALIASES.get(visual_type, {}).items():
        if listed_as in listed:
            listed.setdefault(stored, list(listed[listed_as]))
    return listed


# The schema names an advanced slicer's sections by their Format-pane labels; Desktop stores
# them under other objects. The Agents report keeps its state entries ("default" and
# "selection:selected") on fillCustom and value, which the list calls fill and text.
_STATE_ALIASES = {"advancedSlicerVisual": {"fillCustom": "fill", "value": "text"}}


def needs_default_selector(visual_type: str | None, object_name: str) -> bool:
    """Whether Desktop ignores ``object_name`` of this type unless it sits under
    ``{"id": "default"}``. Measured on a `shape`: a fill written without the selector drew
    in the theme's first color, the same entry with it drew the requested color. Only the
    object/type pairs the schema lists count (a matrix keeps its grid and header
    settings selector-less)."""
    if visual_type is None:
        return False
    return STATE_DEFAULT in instance_selectors(visual_type).get(object_name, [])


def state_selector_for(
    visual_type: str, object_name: str, state: str, objects: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The selector for a visual state (``default``, ``hover``, ``selected``, ``disabled``).

    The state must be one the visual type lists for the object, so the error can name the
    valid ones. An id an existing entry already uses for the state wins over the plain
    name: a slicer tile's selected state is stored as ``selection:selected``, which the
    schema list does not have, and a second ``selected`` entry would be ignored.
    """
    states = instance_selectors(visual_type).get(object_name)
    if not states:
        raise ExpressionError(
            f"{with_article(visual_type)} has no separate states for {object_name}"
        )
    for entry in (objects or {}).get(object_name, []):
        found = (entry.get("selector") or {}).get("id")
        if isinstance(found, str) and found in {state, f"selection:{state}"}:
            return {"id": found}
    if state not in states:
        listed = (
            f"only the state {states[0]!r}"
            if len(states) == 1
            else f"the states {', '.join(states)}"
        )
        raise ExpressionError(
            f"{with_article(visual_type)} {object_name} has {listed}, not {state!r}"
        )
    return {"id": state}


def find_equivalent_selector(
    objects: dict[str, Any], object_name: str, selector: dict[str, Any]
) -> dict[str, Any] | None:
    """The selector of an existing entry that names the same category as ``selector``.

    A category is the same one whichever way its literal is spelled (``2019L`` and
    ``2019D``), so an edit reuses Desktop's entry and its spelling instead of adding a
    second entry that Desktop would read as a different instance.
    """
    wanted = _scope_key(selector)
    if wanted is None:
        return None
    for entry in objects.get(object_name, []):
        found = entry.get("selector")
        if isinstance(found, dict) and _scope_key(found) == wanted:
            return copy.deepcopy(found)
    return None


def _scope_key(selector: dict[str, Any]) -> Any:
    data = selector.get("data")
    if not isinstance(data, list) or len(data) != 1 or selector_kind(selector) != "data":
        return None
    item = data[0]
    comparison = (item.get("scopeId") or {}).get("Comparison") if isinstance(item, dict) else None
    if not isinstance(comparison, dict):
        return None
    right = comparison.get("Right")
    text = _literal_text(right) if isinstance(right, dict) else None
    if text is None:
        return None
    return (json.dumps(comparison.get("Left"), sort_keys=True), _normalized_literal(text))


def set_property(
    objects: dict[str, Any],
    object_name: str,
    prop: str,
    value: dict[str, Any],
    selector: dict[str, Any] | None = None,
    *,
    visual_type: str | None = None,
) -> None:
    """Set one property in the entry with this selector, adding the entry if needed.

    Other properties of the entry (a title's font, size and wrap) are kept: pbi-cli's
    set-container replaced whole entries and lost them. With ``visual_type``, a write
    with no selector to an object that type only honors under a state selector goes under
    ``{"id": "default"}``, also when no entry exists yet.
    """
    if selector is None and needs_default_selector(visual_type, object_name):
        selector = {"id": STATE_DEFAULT}
    entries = objects.setdefault(object_name, [])
    for entry in entries:
        if _same_selector(entry.get("selector"), selector):
            entry.setdefault("properties", {})[prop] = value
            return
    added: dict[str, Any] = {"properties": {prop: value}}
    if selector:
        added["selector"] = copy.deepcopy(selector)
    entries.append(added)


def set_everywhere(
    objects: dict[str, Any],
    object_name: str,
    prop: str,
    value: dict[str, Any],
    *,
    visual_type: str | None = None,
) -> None:
    """Set a property for the whole visual, where Desktop's own entries will read it.

    The new card visual keeps its value style in a {"id": "default"} entry and in
    per-field {"metadata": …} entries; either overrides a plain entry. So the value
    goes into the plain and default entries, and into every narrower entry (per field,
    per category) that already sets the same property. A plain entry added next to
    them changed nothing in Desktop (smoke run, 2026-09-28). With ``visual_type`` the
    object/type pairs that need a default selector get one, also when no entry exists.
    Prefer ``write_everywhere``: this one also overwrites state entries and conditional
    formatting without saying so.
    """
    entries = objects.setdefault(object_name, [])
    general = [e for e in entries if _is_general(e)]
    narrower = [e for e in entries if e not in general and prop in e.get("properties", {})]
    if needs_default_selector(visual_type, object_name) and not any(
        e.get("selector") == {"id": STATE_DEFAULT} for e in entries
    ):
        added: dict[str, Any] = {"properties": {}, "selector": {"id": STATE_DEFAULT}}
        entries.append(added)
        general.append(added)
    elif not general:
        added = {"properties": {}}
        if any("id" in (e.get("selector") or {}) for e in entries):
            added["selector"] = {"id": STATE_DEFAULT}
        entries.append(added)
        general = [added]
    for entry in general + narrower:
        entry.setdefault("properties", {})[prop] = value


class ReplacedEntry(NamedTuple):
    """A narrower entry ``write_everywhere`` overwrote and the value it had."""

    selector: dict[str, Any] | None
    previous: Any


class ProtectedEntry(NamedTuple):
    """An entry ``write_everywhere`` left alone because its value is not a plain literal."""

    selector: dict[str, Any] | None
    kind: str


@dataclass
class WriteResult:
    """What ``write_everywhere`` did, so chat can say what changed and what it kept."""

    written: int = 0
    replaced: list[ReplacedEntry] = field(default_factory=list)
    protected: list[ProtectedEntry] = field(default_factory=list)
    # State entries (selected, hover, ...) that set the property and were left as they are:
    # a whole-visual write changes the default look, as Desktop's Format pane does.
    kept_states: list[dict[str, Any]] = field(default_factory=list)


_EXPRESSION_KINDS = {
    "Measure": "measure",
    "FillRule": "gradient",
    "Conditional": "conditional",
    "Aggregation": "aggregation",
    "Column": "field",
}


def protected_kind(value: Any) -> str | None:
    """None for a plain value (a literal or a theme color), else what the value is.

    A table's ``values`` object keeps four ``fontColor`` entries as Measure expressions
    (conditional formatting); a literal written over one destroys the rule. So only
    ``Literal`` and ``ThemeDataColor`` expressions, bare or under ``solid.color``, count
    as plain, and anything unrecognized is protected too.
    """
    node = value
    if isinstance(node, dict) and "solid" in node:
        solid = node["solid"]
        node = solid.get("color") if isinstance(solid, dict) else None
    expression = node.get("expr") if isinstance(node, dict) else None
    if not isinstance(expression, dict) or not expression:
        return "unknown"
    key = next(iter(expression))
    if key in {"Literal", "ThemeDataColor"}:
        return None
    return _EXPRESSION_KINDS.get(str(key), str(key).lower())


def _is_general(entry: dict[str, Any]) -> bool:
    return not entry.get("selector") or entry.get("selector") == {"id": STATE_DEFAULT}


def write_everywhere(
    objects: dict[str, Any],
    object_name: str,
    prop: str,
    value: dict[str, Any] | Callable[[Any], dict[str, Any]],
    *,
    replace_conditional: bool = False,
    visual_type: str | None = None,
) -> WriteResult:
    """``set_everywhere`` that keeps conditional formatting and reports what it replaced.

    The value goes into the plain and default entries and into every narrower entry that
    already sets the property, as ``set_everywhere`` does. An entry whose current value is
    not a plain literal (a Measure, FillRule, Conditional or Aggregation expression) is
    left alone and listed in ``protected`` unless ``replace_conditional`` is true, in which
    case it is overwritten and listed in ``replaced`` with the expression it held. Every
    narrower entry overwritten with a different value is listed in ``replaced`` with its
    selector and previous value ("replaces TopN blue, BottomN red, Others grey"). State
    entries (``selection:selected``, hover) are not part of "the whole visual": they are
    left as they are and listed in ``kept_states``.

    With ``visual_type``, an object that type only honors under a state selector
    (``needs_default_selector``) gets a ``{"id": "default"}`` entry when it has none, also
    when the object has no entry at all, and the write goes to that entry and to selector-
    less entries that already set the property, not to selector-less entries that do not.

    ``value`` may be a function of the entry's current expression (None when the entry does
    not set the property yet) that returns the expression to write: a relative change ("two
    points bigger", "darker") then moves each entry from its own value instead of copying
    one entry's result over the others. It runs only for entries that are written.
    """
    entries = objects.setdefault(object_name, [])
    required = needs_default_selector(visual_type, object_name)
    if required:
        general = [
            e
            for e in entries
            if e.get("selector") == {"id": STATE_DEFAULT}
            or (not e.get("selector") and prop in e.get("properties", {}))
        ]
    else:
        general = [e for e in entries if _is_general(e)]
    general_ids = {id(e) for e in general}
    narrower = [e for e in entries if id(e) not in general_ids and prop in e.get("properties", {})]
    result = WriteResult()
    states = [e for e in narrower if selector_kind(e.get("selector")) == "state"]
    for entry in states:
        result.kept_states.append(copy.deepcopy(entry["selector"]))
    narrower = [e for e in narrower if e not in states]
    has_default = any(e.get("selector") == {"id": STATE_DEFAULT} for e in entries)
    if (required and not has_default) or (not required and not general):
        added: dict[str, Any] = {"properties": {}}
        if required or any("id" in (e.get("selector") or {}) for e in entries):
            added["selector"] = {"id": STATE_DEFAULT}
        entries.append(added)
        general = [added, *general]
        general_ids.add(id(added))
    for entry in general + narrower:
        properties = entry.setdefault("properties", {})
        current = properties.get(prop)
        selector = copy.deepcopy(entry.get("selector")) or None
        kind = protected_kind(current) if current is not None else None
        if kind is not None and not replace_conditional:
            result.protected.append(ProtectedEntry(selector, kind))
            continue
        new = value(current) if callable(value) else value
        if current is not None and current != new:
            decoded = decode(current)
            if id(entry) not in general_ids or kind is not None:
                result.replaced.append(
                    ReplacedEntry(selector, current if decoded is None else decoded)
                )
        properties[prop] = copy.deepcopy(new)
        result.written += 1
    return result


class CoveringEntry(NamedTuple):
    """An object entry whose selector covers a target, with how specifically it does."""

    entry: dict[str, Any]
    kind: str
    rank: int
    position: int


class EffectiveValue(NamedTuple):
    """The entry that wins for a target and the value it holds, decoded."""

    entry: dict[str, Any]
    value: Any

    @property
    def kind(self) -> str:
        return selector_kind(self.entry.get("selector"))


# Most specific first. A state entry ("selection:selected", a reference label's
# "field-<uuid>") only covers a target in that state, and then beats everything else.
_RANK = {"none": 0, "default": 1, "metadata": 2, "data": 3, "data+metadata": 4, "state": 5}


def _normalized_literal(text: str) -> Any:
    # 2019L on an entry and 2019D on a target are the same instance.
    # Integers stay ints: through float, 2**53 + 1 would equal 2**53 and two different
    # 64-bit keys would look like one instance.
    match = _NUMBER.fullmatch(text)
    return _parse_number(match.group(1), match.group(2)) if match else text


def _scope_literal(item: Any) -> str | None:
    comparison = (item.get("scopeId") or {}).get("Comparison") if isinstance(item, dict) else None
    right = comparison.get("Right") if isinstance(comparison, dict) else None
    return _literal_text(right) if isinstance(right, dict) else None


def _data_covers(selector_data: Any, target_data: Any, column_scoped: bool = False) -> bool:
    if not isinstance(selector_data, list):
        return False
    for item in selector_data:
        if not isinstance(item, dict):
            return False
        wildcard = item.get("dataViewWildcard")
        if isinstance(wildcard, dict):
            # matchingOption 2 is totals only: it paints no ordinary instance.
            if wildcard.get("matchingOption") == 2:
                return False
            # Any other wildcard matches every instance of its column, so with a metadata
            # match it covers a target that names only the column (the table "values"
            # rules, {data:[wildcard], metadata:'# Measures.Rank'}). A bare wildcard still
            # needs the target to name an instance, or it would outrank the column entry.
            if not target_data and not column_scoped:
                return False
            continue
        if not target_data:
            return False
        wanted = _scope_literal(item)
        matched = False
        for candidate in target_data:
            if isinstance(candidate, str):
                matched = wanted is not None and _normalized_literal(
                    candidate
                ) == _normalized_literal(wanted)
            else:
                matched = same_selector(item, candidate)
            if matched:
                break
        if not matched:
            return False
    return True


def _covers(selector: dict[str, Any] | None, target: dict[str, Any]) -> bool:
    kind = selector_kind(selector)
    if selector is None or kind in {"none", "default"}:
        return True
    if kind in {"state", "field"}:
        if target.get("state") != selector.get("id"):
            return False
        wanted = selector.get("metadata")
        return not (wanted and target.get("metadata") and target["metadata"] != wanted)
    if kind in {"metadata", "data+metadata"} and target.get("metadata") != selector["metadata"]:
        return False
    if kind in {"data", "data+metadata"}:
        return _data_covers(selector.get("data"), target.get("data"), kind == "data+metadata")
    return True


def entries_covering(
    objects: dict[str, Any],
    object_name: str,
    target: dict[str, Any] | None,
    prop: str | None = None,
) -> list[CoveringEntry]:
    """The entries of an object whose selector covers ``target``, winner first.

    ``target`` describes one instance: ``{"metadata": queryRef, "data": [scopeId literals
    or data items], "state": id}``, every key optional. Specificity ranks data+metadata >
    data (scopeId or wildcard) > metadata > ``{"id": "default"}`` > no selector, and a
    state entry beats all of them for a target in that state; equal ranks resolve to the
    later entry. With ``prop``, only entries that set it are returned.
    """
    wanted = target or {}
    covering = []
    for index, entry in enumerate(objects.get(object_name, [])):
        if prop is not None and prop not in entry.get("properties", {}):
            continue
        selector = entry.get("selector") or None
        if not _covers(selector, wanted):
            continue
        kind = selector_kind(selector)
        rank = _RANK["state"] if kind == "field" else _RANK[kind]
        covering.append(CoveringEntry(entry, kind, rank, index))
    covering.sort(key=lambda c: (c.rank, c.position), reverse=True)
    return covering


def effective_property(
    objects: dict[str, Any], object_name: str, prop: str, target: dict[str, Any] | None
) -> EffectiveValue | None:
    """The entry Desktop reads ``prop`` from for ``target`` and its decoded value, or None."""
    found = entries_covering(objects, object_name, target, prop)
    if not found:
        return None
    entry = found[0].entry
    return EffectiveValue(entry, decode(entry["properties"][prop]))


def remove_property(
    objects: dict[str, Any], object_name: str, prop: str, selector: dict[str, Any] | None = None
) -> bool:
    removed = False
    for entry in objects.get(object_name, []):
        if _same_selector(entry.get("selector"), selector):
            removed = entry.get("properties", {}).pop(prop, None) is not None or removed
    objects[object_name] = [e for e in objects.get(object_name, []) if e.get("properties")]
    if not objects[object_name]:
        objects.pop(object_name)
    return removed


def get_property(
    objects: dict[str, Any], object_name: str, prop: str, selector: dict[str, Any] | None = None
) -> Any:
    for entry in objects.get(object_name, []):
        if _same_selector(entry.get("selector"), selector) and prop in entry.get("properties", {}):
            return entry["properties"][prop]
    return None


# --- the catalog --------------------------------------------------------------------

# Read through importlib.resources like report_capabilities does, so the file is found in the
# packaged app (build-desktop-sidecar.ps1 adds it as data) and not only next to a source checkout.
_CATALOG = "pbir_catalog.json"
# The catalog's "formatting" type hides what the value is; its `subtype` (kept from
# capabilities.json by scripts/snapshot_formatting_catalog.py) says. Numbers are doubles
# (fontSize 34D, labelDisplayUnits 1000D); these are text. dashArray and stylePreset have
# not been seen in a file Desktop wrote, so they are refused rather than guessed.
_NUMERIC_SUBTYPES = frozenset({"fontSize", "labelDisplayUnits", "displayUnitsWithoutAuto"})
_TEXT_SUBTYPES = frozenset({"fontFamily", "alignment", "formatString", "altText"})


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    text = files("ai_powerbi_analyst.studio").joinpath(_CATALOG).read_text(encoding="utf-8")
    loaded: dict[str, Any] = json.loads(text)
    return loaded


def visual_types() -> set[str]:
    return set(catalog()["visualTypes"])


def roles(visual_type: str) -> dict[str, Any]:
    entry: dict[str, Any] | None = catalog()["visualTypes"].get(visual_type)
    if entry is None:
        raise ExpressionError(f"Unknown visual type {visual_type!r}")
    return entry


def role_order(visual_type: str) -> list[str]:
    """The visual type's data roles in Power BI's authoring order (Category, Series, Y...).

    ``roles(visual_type)["roles"]`` is a name mapping; this is the order the field wells
    fill in, which the old alphabetical snapshot lost (KPI came out Goal, Indicator,
    TrendLine and the value landed in the target well).
    """
    entry = roles(visual_type)
    ordered = entry.get("roleOrder")
    return [str(name) for name in ordered] if ordered else list(entry["roles"])


def property_spec(
    visual_type: str, object_name: str, prop: str
) -> tuple[str, dict[str, Any]] | None:
    """(where, spec): where is "objects" or "visualContainerObjects".

    A visual type the catalog does not know (a custom visual) still has the container
    objects every visual has: title, background, border, shadow, padding.
    """
    entry = catalog()["visualTypes"].get(visual_type)
    if entry is None:
        spec = catalog()["containerObjects"].get(object_name, {}).get(prop)
        return ("visualContainerObjects", spec) if spec is not None else None
    spec = entry["objects"].get(object_name, {}).get(prop)
    if spec is not None:
        return "objects", spec
    if object_name in entry["containerObjects"]:
        spec = catalog()["containerObjects"].get(object_name, {}).get(prop)
        if spec is not None:
            return "visualContainerObjects", spec
    return None


def _check_range(prop: str, number: float | int, subtype: str | None) -> None:
    """Refuse a size or a precision Power BI cannot draw (a negative font, 1e308 points)."""
    lowered = prop.casefold()
    if subtype == "fontSize" or lowered.endswith("fontsize") or lowered == "textsize":
        if not 1 <= number <= 500:
            raise ExpressionError(f"{prop} must be between 1 and 500, not {number:g}")
    elif lowered.endswith("precision") and not 0 <= number <= 15:
        raise ExpressionError(f"{prop} must be between 0 and 15 decimals, not {number:g}")


def _number_expression(
    value: Any, prop: str, existing: Any, default: str, subtype: str | None = None
) -> dict[str, Any]:
    # Desktop's own spelling wins over the catalog: accentBar.width is "numeric" there but
    # Desktop wrote 6L, so an overwrite keeps the suffix of what is already in the file.
    suffix = number_suffix(existing) or default
    number = _number(value)
    if suffix == "L":
        if not float(number).is_integer():
            raise ExpressionError(f"{prop} must be a whole number")
        _long(number)
    else:
        _sane(number)
    _check_range(prop, number, subtype)
    return _literal(f"{int(number) if suffix == 'L' else _plain(number)}{suffix}")


def encode_property(
    spec: dict[str, Any], prop: str, value: Any, existing: Any = None
) -> dict[str, Any]:
    """The expression for ``value`` as this catalog property's type.

    ``existing`` is the expression Desktop already wrote at this property, if any: a
    number is written with the same D or L suffix.
    """
    kind = spec.get("type")
    if kind == "bool":
        return boolean(value)
    if kind == "numeric":
        return _number_expression(value, prop, existing, "D")
    if kind == "integer":
        return _number_expression(value, prop, existing, "L")
    if kind == "fill":
        if isinstance(value, dict) and "theme" in value:
            return theme_color(int(value["theme"]), float(value.get("percent", 0)))
        return color(value)
    if kind == "enum":
        allowed = [str(v) for v in spec.get("values") or []]
        text = str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)
        if allowed and text not in allowed:
            raise ExpressionError(f"{prop} must be one of {', '.join(allowed)}")
        # Numeric enums (display units, orientation) are numbers: the Agents report
        # stores a slicer's orientation as 2D.
        if allowed and all(re.fullmatch(r"-?\d+", v) for v in allowed):
            return _number_expression(text, prop, existing, "D")
        return string(text)
    if kind == "text":
        return string(value)
    if kind == "formatting":
        subtype = spec.get("subtype")
        if subtype is None:
            # A spec from a catalog snapshot made before subtypes were kept: fontSize 16D
            # and textSize 11D are numbers, fontFamily, alignment and formatString text.
            if prop.casefold().endswith("size"):
                return _number_expression(value, prop, existing, "D")
            return string(value)
        if subtype in _NUMERIC_SUBTYPES:
            return _number_expression(value, prop, existing, "D", str(subtype))
        if subtype == "fontFamily":
            if not isinstance(value, str):
                raise ExpressionError("Text values must be strings")
            return _literal(font_literal(value).text)
        if subtype in _TEXT_SUBTYPES:
            return string(value)
    raise ExpressionError(f"Studio cannot set {prop} (type {kind!r}) yet")
