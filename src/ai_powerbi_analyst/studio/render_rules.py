"""Conditional formatting and visual-level number formats, applied the way Power BI does.

Desktop writes a rule-driven color in place of a literal: ``{"solid": {"color": {"expr": X}}}``
where X is a ``Measure`` (field value: the measure returns the color), a ``FillRule`` (a 2- or
3-color gradient over an input value) or a ``Conditional`` (rules: the first case whose
condition holds gives the color). Icons are a ``Conditional`` whose cases give an icon name.
None of these can be drawn without the data, so the renderer turns each one into a ``Rule``
over one field of the visual's query and evaluates it per row after the query (``evaluate``).

A rule is parsed from the file alone (the same for the data render and the formatting-only
render); anything it cannot evaluate exactly raises ``RuleUnsupported`` and the visual is
drawn with the plain value and marked approximate.

``display_format`` turns visual-level display units and decimals into the format string,
divisor and suffix the query formats a value with.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from . import pbir_expr
from .report_props import color, expand_hex, safe_color

Json = dict[str, Any]
# The field index that holds an input expression's value in every row, or None when the
# expression cannot be added to the visual's query (a missing field, the 12-field limit).
Resolve = Callable[[Json], int | None]

# Shapes read here that no Desktop-authored file in hand shows. The lead's Desktop probes
# decide them; until then a visual using them renders from what the published schemas say.
UNVERIFIED_SHAPES: dict[str, str] = {
    "FillRule.linearGradient2": (
        "Stops as unwrapped literals ({min: {color: {Literal}, value?: {Literal}}}); a stop "
        "without value means the lowest/highest value in the data. Interpolated in RGB."
    ),
    "FillRule.linearGradient3": (
        "As linearGradient2 with a mid stop; a mid without value is the midpoint"
    ),
    "FillRule.nullColoringStrategy": "{strategy: 'asZero'|'specificColor'|'noColor', color?}",
    "Conditional": (
        "Cases of Comparison/And/Or/Not over one input expression against number, text or "
        "boolean literals, a 'null' literal (Equal: is blank) or RangePercent{Min, Max, "
        "Percent} (a bound relative to the lowest and highest value of the visual's rows); "
        "a catch-all case is Literal true; ComparisonKind 0 =, 1 >, 2 >=, 3 <, 4 <=; "
        "DefaultValue optional"
    ),
    "values.icon": (
        "{kind: 'Icon', layout: 'Before'|'After'|'IconOnly', verticalAlignment, "
        "value: {expr: Conditional with icon-name literals}}"
    ),
    "columnFormatting.dataBars.minValue/maxValue": "Literal bounds of the bar; absent = the data",
    "columnFormatting.dataBars.totalMatchingOption": (
        "0 = the values and the totals row, 2 = the totals only (as dataViewWildcard's "
        "matchingOption); Studio draws no bar on a totals row, so either is listed unrendered"
    ),
    "dataPoint.fill rule selector": (
        "A chart's per-point color rule under no selector, {id: 'default'}, a metadata "
        "selector (one series) or dataViewWildcard data with matchingOption 0 or 1; "
        "matchingOption 2 (totals only) colors no point"
    ),
    "cardVisual value/label/fillCustom rule selector": (
        "A new card's conditional value color, label color and fill under data "
        "[dataViewWildcard] with metadata naming the measure, evaluated on the card's one row"
    ),
    "card labels.color rule": "The legacy card's conditional value color under labels.color",
    "values.backColor/fontColor/icon matchingOption": (
        "1 = the value cells only; 0 = the cells and the totals row, 2 = the totals only: "
        "Studio formats no totals row, so 0 is drawn on the cells and listed unrendered and "
        "2 is not drawn"
    ),
    "ThemeDataColor in a rule": (
        "{ThemeDataColor: {ColorId, Percent}}: ColorId 0 white, 1 black, 2.. the theme's "
        "dataColors; resolved when the rows are computed, so the theme's palette is part of "
        "the data key of a visual whose rule uses one"
    ),
}


class RuleUnsupported(ValueError):
    """A conditional format Studio cannot evaluate; the plain value is drawn instead."""


@dataclass(frozen=True)
class Stop:
    color: str
    value: float | None  # None: the lowest, highest or middle value of the data


Condition = tuple[Any, ...]


@dataclass(frozen=True)
class Rule:
    """One conditional format over the values of one field (``input``) of the visual."""

    kind: Literal["field", "gradient", "rules"]
    input: int
    output: Literal["color", "icon"] = "color"
    stops: tuple[Stop, ...] = ()
    null_strategy: str = "asZero"
    null_color: str | None = None
    cases: tuple[tuple[Condition, str], ...] = ()
    default: str | None = None

    @property
    def style(self) -> str:
        """The name the payload gives the rule: ``fieldValue``, ``gradient`` or ``rules``."""
        return {"field": "fieldValue", "gradient": "gradient", "rules": "rules"}[self.kind]

    @property
    def range_dependent(self) -> bool:
        """Whether a row's color depends on the other rows: a scale stop at the lowest or
        highest value, a rule bound given as a percent of the range."""
        if self.kind == "gradient":
            return any(stop.value is None for stop in self.stops)
        return self.kind == "rules" and any(_relative(cond) for cond, _ in self.cases)


def _relative(condition: Condition) -> bool:
    if condition[0] == "pct":
        return True
    return any(isinstance(part, tuple) and _relative(part) for part in condition[1:])


_ICON = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")
_COMPARISONS: dict[int, Callable[[Any, Any], bool]] = {
    0: lambda a, b: bool(a == b),
    1: lambda a, b: bool(a > b),
    2: lambda a, b: bool(a >= b),
    3: lambda a, b: bool(a < b),
    4: lambda a, b: bool(a <= b),
}


def expression_of(node: Any) -> Json | None:
    """The expression a property holds: ``{solid: {color: {expr: X}}}`` or ``{expr: X}``."""
    if not isinstance(node, dict):
        return None
    inner: Any = node
    if "solid" in node:
        solid = node["solid"]
        inner = solid.get("color") if isinstance(solid, dict) else None
    found = inner.get("expr") if isinstance(inner, dict) else None
    return found if isinstance(found, dict) and len(found) == 1 else None


def is_rule(node: Any) -> bool:
    """True for a property whose value comes from data (field value, gradient, rules)."""
    found = expression_of(node)
    return found is not None and next(iter(found)) in {"Measure", "FillRule", "Conditional"}


def _stop_color(node: Any, palette: Sequence[str]) -> str:
    # Gradient stops and rule values are unwrapped expressions ({Literal} or
    # {ThemeDataColor}); a wrapped one ({expr: ...}) is read the same way.
    if isinstance(node, dict) and "ThemeDataColor" in node:
        node = {"expr": node}
    found = color(node, list(palette))
    # Blending needs #rrggbb: a named or 3-digit color is expanded, an alpha is dropped.
    normalized = expand_hex(found) if found else None
    if normalized is None and found and re.fullmatch(r"#[0-9a-fA-F]{8}", found):
        normalized = found[:7].lower()
    if normalized is None:
        raise RuleUnsupported("A color of this rule is not a color Studio can read")
    return normalized


def _number(node: Any) -> float:
    found = pbir_expr.decode(node)
    if isinstance(found, bool) or not isinstance(found, int | float) or not math.isfinite(found):
        raise RuleUnsupported("A bound of this rule is not a number")
    return float(found)


def color_rule(node: Any, resolve: Resolve, palette: Sequence[str]) -> Rule | None:
    """The rule a color property holds, None for a plain color, ``RuleUnsupported`` for a
    rule Studio cannot evaluate."""
    found = expression_of(node)
    if found is None:
        return None
    kind, body = next(iter(found.items()))
    if kind in {"Literal", "ThemeDataColor"}:
        return None
    if kind == "Measure":
        return Rule("field", _input(found, resolve))
    if kind == "FillRule" and isinstance(body, dict):
        return _gradient(body, resolve, palette)
    if kind == "Conditional" and isinstance(body, dict):
        return _conditional(body, resolve, palette, "color")
    raise RuleUnsupported("This color comes from data in a way Studio cannot evaluate")


def icon_rule(node: Any, resolve: Resolve) -> tuple[Rule, str, str] | None:
    """(rule, layout, vertical alignment) of an ``icon`` property, None when it has none.

    The layout is ``before``, ``after`` or ``only`` (icon only), the alignment ``top``,
    ``middle`` or ``bottom``.
    """
    if not isinstance(node, dict):
        return None
    value = node.get("value", node)
    found = expression_of(value) if isinstance(value, dict) else None
    if found is None:
        return None
    kind, body = next(iter(found.items()))
    if kind == "Measure":
        rule = Rule("field", _input(found, resolve), output="icon")
    elif kind == "Conditional" and isinstance(body, dict):
        rule = _conditional(body, resolve, [], "icon")
    else:
        raise RuleUnsupported("These icons come from data in a way Studio cannot evaluate")
    raw_layout = str(pbir_expr.decode(node.get("layout")) or "Before").lower()
    layout = {"before": "before", "after": "after", "icononly": "only"}.get(raw_layout, "before")
    raw_align = str(pbir_expr.decode(node.get("verticalAlignment")) or "Top").lower()
    align = raw_align if raw_align in {"top", "middle", "bottom"} else "top"
    return rule, layout, align


def _input(node: Any, resolve: Resolve) -> int:
    if not isinstance(node, dict) or len(node) != 1:
        raise RuleUnsupported("The field this rule is based on cannot be read")
    index = resolve(node)
    if index is None:
        raise RuleUnsupported("The field this rule is based on cannot be added to the preview")
    return index


def _gradient(body: Json, resolve: Resolve, palette: Sequence[str]) -> Rule:
    fill = body.get("FillRule")
    if not isinstance(fill, dict):
        raise RuleUnsupported("This color scale has no stops")
    gradients = (("linearGradient2", ("min", "max")), ("linearGradient3", ("min", "mid", "max")))
    for key, names in gradients:
        spec = fill.get(key)
        if not isinstance(spec, dict):
            continue
        stops: list[Stop] = []
        for name in names:
            stop = spec.get(name)
            if not isinstance(stop, dict):
                raise RuleUnsupported("A stop of this color scale is missing")
            bound = stop.get("value")
            at = None if bound is None else _number(bound)
            stops.append(Stop(_stop_color(stop.get("color"), palette), at))
        strategy, null_color = "asZero", None
        null = spec.get("nullColoringStrategy")
        if isinstance(null, dict):
            strategy = str(pbir_expr.decode(null.get("strategy")) or "asZero")
            if strategy == "specificColor":
                null_color = _stop_color(null.get("color"), palette)
        return Rule(
            "gradient",
            _input(body.get("Input"), resolve),
            stops=tuple(stops),
            null_strategy=strategy,
            null_color=null_color,
        )
    raise RuleUnsupported("This color scale is not a 2- or 3-color gradient")


def _conditional(
    body: Json, resolve: Resolve, palette: Sequence[str], output: Literal["color", "icon"]
) -> Rule:
    raw_cases = body.get("Cases")
    if not isinstance(raw_cases, list) or not raw_cases or len(raw_cases) > 50:
        raise RuleUnsupported("These rules have no cases Studio can read")
    lefts: list[Json] = []
    cases: list[tuple[Condition, str]] = []
    for case in raw_cases:
        if not isinstance(case, dict):
            raise RuleUnsupported("A rule is malformed")
        condition = _condition(case.get("Condition"), lefts, 0)
        cases.append((condition, _outcome(case.get("Value"), palette, output)))
    default_node = body.get("DefaultValue")
    default = None if default_node is None else _outcome(default_node, palette, output)
    if not lefts:
        # Only "any value" cases: the outcome does not depend on a field.
        return Rule("rules", 0, output=output, cases=tuple(cases), default=default)
    left = lefts[0]
    if any(other != left for other in lefts[1:]):
        # Every case compares the same value in what Desktop writes; two inputs is not a
        # rule Studio can evaluate from one column.
        raise RuleUnsupported("These rules compare more than one field")
    return Rule("rules", _input(left, resolve), output=output, cases=tuple(cases), default=default)


def _outcome(node: Any, palette: Sequence[str], output: Literal["color", "icon"]) -> str:
    if output == "color":
        return _stop_color(node, palette)
    found = pbir_expr.decode(node)
    if not isinstance(found, str) or not _ICON.fullmatch(found):
        raise RuleUnsupported("An icon of these rules is not a built-in icon name")
    return found


def _condition(node: Any, lefts: list[Json], depth: int) -> Condition:
    if depth > 8 or not isinstance(node, dict):
        raise RuleUnsupported("A rule condition is too deep or malformed")
    for name in ("And", "Or"):
        pair = node.get(name)
        if isinstance(pair, dict):
            return (
                name.lower(),
                _condition(pair.get("Left"), lefts, depth + 1),
                _condition(pair.get("Right"), lefts, depth + 1),
            )
    inner = node.get("Not")
    if isinstance(inner, dict):
        return ("not", _condition(inner.get("Expression"), lefts, depth + 1))
    if "Literal" in node:
        # A rule without bounds ("any value"): Desktop writes the condition as true.
        constant = pbir_expr.decode(node)
        if isinstance(constant, bool):
            return ("const", constant)
        raise RuleUnsupported("A rule condition is not true/false")
    comparison = node.get("Comparison")
    if isinstance(comparison, dict):
        kind = comparison.get("ComparisonKind")
        left, right = comparison.get("Left"), comparison.get("Right")
        if kind not in _COMPARISONS or not isinstance(left, dict) or not isinstance(right, dict):
            raise RuleUnsupported("A rule compares in a way Studio cannot evaluate")
        lefts.append(left)
        percent = right.get("RangePercent")
        if isinstance(percent, dict):
            # Desktop's percent bound: lowest + p * (highest - lowest) of the rule's input
            # over everything the visual shows.
            fraction = percent.get("Percent")
            if isinstance(fraction, bool) or not isinstance(fraction, int | float):
                raise RuleUnsupported("A percent bound of this rule cannot be read")
            return ("pct", kind, float(fraction))
        literal = right.get("Literal")
        if not isinstance(literal, dict):
            raise RuleUnsupported("A rule bound is an expression Studio cannot evaluate")
        if literal.get("Value") == "null":
            if kind != 0:
                raise RuleUnsupported("A rule compares a blank in a way Studio cannot evaluate")
            return ("blank",)
        value = pbir_expr.decode(right)
        if value is None or isinstance(value, datetime):
            raise RuleUnsupported("A rule bound is not a number, text or true/false")
        return ("cmp", kind, value)
    raise RuleUnsupported("A rule condition is not a comparison")


def _holds(condition: Condition, value: Any, span: tuple[float, float] | None = None) -> bool:
    """Whether ``condition`` holds for ``value``; ``span`` is the lowest and highest input
    value of the visual, which percent bounds are relative to."""
    head = condition[0]
    if head == "and":
        return _holds(condition[1], value, span) and _holds(condition[2], value, span)
    if head == "or":
        return _holds(condition[1], value, span) or _holds(condition[2], value, span)
    if head == "not":
        return not _holds(condition[1], value, span)
    if head == "const":
        return bool(condition[1])
    if head == "blank":
        return value is None
    bound = condition[2]
    if head == "pct":
        if span is None:
            return False
        bound = span[0] + bound * (span[1] - span[0])
    if value is None:
        # A blank is not formatted by a value rule.
        return False
    numeric = isinstance(bound, int | float) and not isinstance(bound, bool)
    if numeric != (isinstance(value, int | float) and not isinstance(value, bool)):
        return False
    if isinstance(bound, str) and isinstance(value, str):
        # Text compares without regard to case, as DAX does.
        bound, value = bound.casefold(), value.casefold()
    try:
        return _COMPARISONS[int(condition[1])](value, bound)
    except TypeError:
        return False


# The public name number_explain uses to say which case of a rule holds for a row.
holds = _holds


# --- evaluation --------------------------------------------------------------------------


def _numeric(cell: Any) -> float | None:
    if isinstance(cell, bool) or not isinstance(cell, int | float):
        return None
    return float(cell) if math.isfinite(cell) else None


def _channels(hex_color: str) -> tuple[int, int, int]:
    text = hex_color.lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def blend(low: str, high: str, fraction: float) -> str:
    """The color ``fraction`` (0..1) of the way from ``low`` to ``high``, per RGB channel,
    rounded half up like Power BI's color math."""
    t = max(0.0, min(1.0, fraction))
    mixed = [
        math.floor(a + (b - a) * t + 0.5)
        for a, b in zip(_channels(low), _channels(high), strict=True)
    ]
    return "#" + "".join(f"{max(0, min(255, c)):02x}" for c in mixed)


def _gradient_color(rule: Rule, value: float, low: float, high: float) -> str:
    stops = list(rule.stops)
    positions = [
        stop.value
        if stop.value is not None
        else (low if i == 0 else high if i == len(stops) - 1 else (low + high) / 2)
        for i, stop in enumerate(stops)
    ]
    if value <= positions[0]:
        return stops[0].color
    for i in range(1, len(stops)):
        if value <= positions[i]:
            span = positions[i] - positions[i - 1]
            fraction = (value - positions[i - 1]) / span if span > 0 else 1.0
            return blend(stops[i - 1].color, stops[i].color, fraction)
    return stops[-1].color


def evaluate(rule: Rule, rows: Sequence[Sequence[Any]]) -> list[str | None]:
    """The color (``#rrggbb``) or icon name the rule gives each row, None for no format."""
    cells = [row[rule.input] if rule.input < len(row) else None for row in rows]
    if rule.kind == "field":
        if rule.output == "icon":
            return [c if isinstance(c, str) and _ICON.fullmatch(c) else None for c in cells]
        return [safe_color(c) for c in cells]
    if rule.kind == "rules":
        numbers_seen = [n for n in (_numeric(c) for c in cells) if n is not None]
        span = (min(numbers_seen), max(numbers_seen)) if numbers_seen else None
        found: list[str | None] = []
        for cell in cells:
            value = _numeric(cell) if not isinstance(cell, str | bool) else cell
            match = next((out for cond, out in rule.cases if _holds(cond, value, span)), None)
            found.append(match if match is not None else rule.default)
        return found
    numbers = [_numeric(c) for c in cells]
    as_zero = rule.null_strategy == "asZero"
    # Power BI's lowest and highest values ignore blanks; a blank colored "as zero" takes the
    # color of 0 within that range (clamped to the ends).
    present = [n for n in numbers if n is not None]
    low, high = (min(present), max(present)) if present else (0.0, 0.0)
    result: list[str | None] = []
    for number in numbers:
        if number is None:
            result.append(_gradient_color(rule, 0.0, low, high) if as_zero else rule.null_color)
        else:
            result.append(_gradient_color(rule, number, low, high))
    return result


# --- number formats ----------------------------------------------------------------------

# Power BI's display unit suffixes (en-US): thousands K, millions M, billions bn, trillions T.
UNIT_SUFFIXES: dict[float, str] = {1e3: "K", 1e6: "M", 1e9: "bn", 1e12: "T"}
_PLACEHOLDERS = re.compile(r"[#0][#0,]*(?:\.[#0]*)?|\.[#0]+")


def format_sections(fmt: str) -> list[str]:
    """A format string's ``;`` sections (positive; negative; zero), quotes and escapes kept."""
    sections: list[str] = []
    current: list[str] = []
    quoted = escaped = False
    for char in fmt:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            sections.append("".join(current))
            current = []
            continue
        current.append(char)
    sections.append("".join(current))
    return sections


def _literal_mask(section: str) -> list[bool]:
    """For each character, whether it is literal text (quoted or backslash-escaped)."""
    mask: list[bool] = []
    quoted = escaped = False
    for char in section:
        if escaped:
            mask.append(True)
            escaped = False
        elif char == "\\":
            mask.append(True)
            escaped = True
        elif char == '"':
            mask.append(True)
            quoted = not quoted
        else:
            mask.append(quoted)
    return mask


def is_percent(fmt: str) -> bool:
    """Whether a format's first section shows a percentage (a ``%`` that is not literal)."""
    section = format_sections(_spelled(fmt))[0]
    return any(
        ch == "%" and not lit for ch, lit in zip(section, _literal_mask(section), strict=True)
    )


def _rewrite(section: str, decimals: int | None, suffix: str) -> str:
    """One section with its digits set to ``decimals`` places and ``suffix`` (quoted) right
    after them. A section without digits (empty, or text such as ``@``) is kept."""
    mask = _literal_mask(section)
    match = next(
        (m for m in _PLACEHOLDERS.finditer(section) if not any(mask[m.start() : m.end()])),
        None,
    )
    if match is None:
        return section
    digits = match.group(0)
    if decimals is not None:
        grouping = "#,0" if "," in digits else "0"
        digits = grouping + ("." + "0" * decimals if decimals else "")
    tail = f'"{suffix}"' if suffix else ""
    return section[: match.start()] + digits + tail + section[match.end() :]


# FORMAT's named number formats, spelled out so their digits can be rewritten.
_NAMED_FORMATS = {
    "general number": "#,0.###############",
    "currency": "$#,##0.00;($#,##0.00)",
    "fixed": "0.00",
    "standard": "#,##0.00",
    "percent": "0.00%",
}


def _spelled(fmt: str) -> str:
    return _NAMED_FORMATS.get(fmt.strip().casefold(), fmt)


def with_suffix(fmt: str, suffix: str) -> str:
    """``fmt`` with a display unit suffix after the digits of every section (so a negative
    section in brackets reads ``($15.4K)``, not ``($15.4)K``)."""
    return ";".join(_rewrite(s, None, suffix) for s in format_sections(_spelled(fmt) or "#,0.##"))


def display_format(
    model_format: str, units: float | None, decimals: int | None
) -> tuple[float, str] | None:
    """(divisor, format string) for visual-level display units and decimals.

    None when neither is set (the model's format applies, and a card keeps its automatic
    units). ``units`` 0 is Auto and 1 is None (no scaling). Every ``;`` section (positive,
    negative, zero) keeps its own text: decimals replace each section's decimal places and
    the unit suffix is written, quoted, after each section's digits. Display units do not
    scale a percentage, as in Power BI.
    """
    if (units is None or units == 0) and decimals is None:
        return None
    model_format = _spelled(model_format)
    base = model_format or "#,0.##"
    divisor, suffix = 1.0, ""
    if units is not None and units > 1 and not is_percent(base):
        if units not in UNIT_SUFFIXES:
            return None
        divisor, suffix = units, UNIT_SUFFIXES[units]
    places = None if decimals is None else max(0, min(15, decimals))
    if not model_format and places is not None:
        base = "#,0"
    return divisor, ";".join(_rewrite(s, places, suffix) for s in format_sections(base))
