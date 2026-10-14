"""Keep a planner's slots faithful to the request before building an operation.

This layer does not execute edits or guess missing business meaning. It corrects an
unambiguous slot from the request and asks when a handler default would drop scope,
an operand, an action or a numeric condition.
"""

from __future__ import annotations

import copy
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .edit_context import EditContext, VisualEntry
from .edit_targets import resolve_visuals
from .edit_text import mask_quotes
from .edit_tools import ToolCard
from .edit_types import Choice, Clause, ParseKind


@dataclass(frozen=True)
class SemanticConstraint:
    values: dict[str, Any]
    clarification: Clause | None = None
    choices: tuple[Choice, ...] = ()


_NO_HIGHLIGHT = frozenset(
    {
        "tableEx",
        "table",
        "pivotTable",
        "card",
        "cardVisual",
        "multiRowCard",
        "slicer",
        "advancedSlicerVisual",
        "kpi",
        "gauge",
    }
)
_NOT_INTERACTIVE = frozenset(
    {
        "textbox",
        "image",
        "shape",
        "basicShape",
        "actionButton",
        "pageNavigator",
        "bookmarkNavigator",
    }
)
_NUMBER = r"(?:zero|[-+]?(?:\d+(?:\.\d*)?|\.\d+))\s*%?"
_CALC_SUBJECT = re.compile(
    r"\b(?:running\s+(?:total|sum)|cumulative\s+(?:total|sum)|moving\s+average|"
    r"rolling\s+average|visual\s+calculation)\s+(?:of|for|on)\s+(.+?)"
    r"(?=\s+(?:to|onto|on|in|into|within|over|across|as|at|by|per)\b|[,;.!?]|$)",
    re.I,
)
_GENERIC_SUBJECT = frozenset(
    {
        "the",
        "a",
        "value",
        "number",
        "data",
        "it",
        "its",
        "their",
        "them",
        "this",
        "these",
        "those",
    }
)
_ALL_PEERS = re.compile(r"\b(?:all|every|other|remaining)\b.{0,25}\bvisuals?\b", re.I)
_BACK = re.compile(r"\b(?:back|go\s+back|return\s+to\s+(?:the\s+)?previous)\s+button\b", re.I)
_DESTINATION = re.compile(
    r"\b(?:navigate(?:s)?\s+to|go(?:es)?\s+to|open(?:s)?|takes?\s+(?:me|us)\s+to)\s+"
    r"(?:the\s+)?(.+?)(?=\s+(?:on|in|onto)\b|[,;.!?]|$)",
    re.I,
)
_ARROW_COUNT = re.compile(
    r"\b(three|four|five|3|4|5)[\s-]+(?:colou?red[\s-]+|gr[ae]y(?:ed)?[\s-]+)?arrows?\b",
    re.I,
)
_COUNT_NAMES = {"three": 3, "four": 4, "five": 5, "3": 3, "4": 4, "5": 5}
_COLORED_ARROWS = {3: "ThreeColoredArrows", 4: "FourColoredArrows", 5: "FiveColoredArrows"}
_COLORED_ARROW_ICONS = {
    3: ("ColoredArrowDown", "ColoredArrowRight", "ColoredArrowUp"),
    4: ("ColoredArrowDown", "ColoredArrowDownRight", "ColoredArrowUpRight", "ColoredArrowUp"),
    5: (
        "ColoredArrowDown",
        "ColoredArrowDownRight",
        "ColoredArrowRight",
        "ColoredArrowUpRight",
        "ColoredArrowUp",
    ),
}


def _ask(values: dict[str, Any], request: str, question: str) -> SemanticConstraint:
    return SemanticConstraint(values, Clause(request, ParseKind.CLARIFY, question=question))


def _contains(text: str, name: str) -> bool:
    if not name.strip():
        return False
    words = re.split(r"\s+", name.strip())
    return bool(re.search(r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + r"(?!\w)", text, re.I))


def _words(text: str) -> set[str]:
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return {
        w[:-1] if w.endswith("s") and len(w) > 3 and w not in {"this", "these"} else w
        for w in re.findall(r"[a-z]+", spaced.casefold())
    }


def _single_value_field(visual: VisualEntry) -> str | None:
    """A category is not a second numeric operand when a chart has one value projection."""
    values: list[str] = []
    state = (visual.definition or {}).get("visual", {}).get("query", {}).get("queryState", {})
    for bucket in state.values():
        for projection in bucket.get("projections", []):
            field = projection.get("field", {})
            measure = (
                field.get("Measure")
                or field.get("Aggregation")
                or field.get("NativeVisualCalculation")
            )
            if not isinstance(measure, Mapping):
                continue
            name = (
                projection.get("nativeQueryRef")
                or projection.get("displayName")
                or measure.get("Property")
            )
            if isinstance(name, str) and name.strip():
                values.append(name)
    if not values:
        values = [name for kind, _table, name in visual.bindings if kind == "measure"]
    values = list(dict.fromkeys(values))
    if len(values) == 1:
        return values[0]
    return visual.fields[0] if not values and len(visual.fields) == 1 else None


def _extra_metric_qualifier(bound: str, requested: str) -> bool:
    # A subset word match does not make Average Sales the requested Sales measure.
    aliases = {"avg": "average", "mean": "average", "min": "minimum", "max": "maximum"}
    qualifiers = {"average", "median", "minimum", "maximum", "count", "distinct"}
    actual = {aliases.get(w, w) for w in _words(bound)} & qualifiers
    wanted = {aliases.get(w, w) for w in _words(requested)} & qualifiers
    return bool(actual - wanted)


def _visual(context: EditContext, request: str, values: dict[str, Any]) -> VisualEntry | None:
    # A field before "on/to the chart" is an operand, not another visual target.
    placements = list(re.finditer(r"\b(?:to|onto|on|in|within)\s+(?:the\s+)?(.+)$", request, re.I))
    target_text = placements[-1].group(1) if placements else request
    named = [v for v in context.visuals if _contains(target_text, v.caption)]
    if named:
        longest = max(len(v.caption) for v in named)
        named = [v for v in named if len(v.caption) == longest]
        return named[0] if len(named) == 1 else None
    resolution = resolve_visuals(target_text, context)
    return resolution.entries[0] if len(resolution.entries) == 1 else None


def _calculation(
    context: EditContext,
    request: str,
    values: dict[str, Any],
) -> SemanticConstraint:
    if not values.get("template"):
        return SemanticConstraint(values)  # explicitly authored expression has its own validator
    visual = _visual(context, request, values)
    if visual is None:
        return _ask(
            values, request, "Which visual should contain the calculation? Select it or name it."
        )
    values["visual"] = visual.id
    bound = tuple(dict.fromkeys(visual.fields))
    match = _CALC_SUBJECT.search(request)
    subject = match.group(1).strip(" '\"[]") if match else None
    if subject and _words(subject) <= _GENERIC_SUBJECT:
        subject = None
    if subject:
        exact = [f for f in bound if f.casefold() == subject.casefold()]
        candidates = exact or [
            f
            for f in bound
            if _words(subject) <= _words(f) and not _extra_metric_qualifier(f, subject)
        ]
        if len(candidates) == 1:
            values["field"] = candidates[0]
            return SemanticConstraint(values)
        if not candidates:
            return _ask(
                values,
                request,
                f"{visual.label} does not show {subject} as a bound field. "
                "Choose a visual that shows it, or add that field to this visual first.",
            )
        return _ask(
            values,
            request,
            f"Which bound field on {visual.label} should the calculation use: "
            f"{', '.join(candidates)}?",
        )
    named = [f for f in bound if _contains(request, f)]
    if len(named) == 1:
        values["field"] = named[0]
        return SemanticConstraint(values)
    if not named and (single := _single_value_field(visual)) is not None:
        values["field"] = single
        return SemanticConstraint(values)
    return _ask(
        values,
        request,
        f"Which bound field on {visual.label} should the calculation use? "
        "Name the values to calculate; another Sales field will not be substituted.",
    )


def _interaction(
    context: EditContext,
    request: str,
    values: dict[str, Any],
) -> SemanticConstraint:
    positive_highlight = re.search(
        r"\bonly\s+(?:cross[- ]?)?highlight\b|\bhighlight(?:ing)?\b.{0,60}"
        r"\b(?:instead\s+of|rather\s+than)\s+filter",
        request,
        re.I,
    )
    negated_highlight = re.search(
        r"\b(?:don't|do\s+not|not|never)\s+(?:want\s+)?(?:only\s+)?highlight\b",
        request,
        re.I,
    )
    if positive_highlight and not negated_highlight:
        values["type"] = "highlight"
    mode = values.get("type")
    if mode != "highlight":
        return SemanticConstraint(values)
    source = context.visual(str(values.get("source", "")))
    if source is None:
        return _ask(
            values, request, "Which visual should do the highlighting? Select it or name it."
        )
    peers = [
        v
        for v in context.visuals_on(source.page_id)
        if v.id != source.id and not v.is_group and v.visual_type not in _NOT_INTERACTIVE
    ]
    by_id = {v.id: v for v in peers}
    explicit = values.get("targets")
    if explicit:
        if not isinstance(explicit, list | tuple) or any(t not in by_id for t in explicit):
            return _ask(values, request, "Which other visuals on the source page should highlight?")
        targets = [by_id[t] for t in explicit]
        if _ALL_PEERS.search(request) and set(explicit) != set(by_id):
            return _ask(
                values,
                request,
                "You asked for all other visuals, but this target list omits "
                "some. Which visuals should highlight, and what should the others do?",
            )
    else:
        targets = peers
    cannot = [v for v in targets if v.visual_type in _NO_HIGHLIGHT]
    if cannot:
        labels = ", ".join(v.label for v in cannot[:5])
        return _ask(
            values,
            request,
            f"{labels} cannot show cross-highlights. Should eligible charts "
            "highlight while interactions with these visuals are disabled, or should "
            "these visuals continue filtering?",
        )
    if not targets:
        return _ask(
            values,
            request,
            "There are no other eligible visuals on this page to highlight. "
            "Which visual or page did you mean?",
        )
    return SemanticConstraint(values)


def _button(context: EditContext, request: str, values: dict[str, Any]) -> SemanticConstraint:
    if not _BACK.search(request):
        return SemanticConstraint(values)
    destination = _DESTINATION.search(request)
    if destination:
        pages = [p for p in context.pages if _contains(destination.group(1), p.name)]
        if len(pages) != 1:
            return _ask(
                values,
                request,
                "Which existing page should the button open? "
                "The page where the button is placed is separate from its destination.",
            )
        if isinstance(values.get("action"), Mapping) or "op" in values:
            values["action"] = {"type": "page", "page": pages[0].id}
            values.pop("target_page", None)
        else:
            values["action"] = "page"
            values["target_page"] = pages[0].id
        return SemanticConstraint(values)
    values["kind"] = "back"
    if isinstance(values.get("action"), Mapping):
        values["action"] = {"type": "back"}
    else:
        values["action"] = "back"
    for key in ("target_page", "target_bookmark", "url"):
        values.pop(key, None)
    return SemanticConstraint(values)


@dataclass(frozen=True)
class _Range:
    minimum: float | None = None
    maximum: float | None = None
    min_inclusive: bool = False
    max_inclusive: bool = False


def _number(text: str) -> float:
    cleaned = text.strip().rstrip("%").strip()
    return (0 if cleaned.casefold() == "zero" else float(cleaned)) / (
        100 if text.strip().endswith("%") else 1
    )


_BOUND = re.compile(
    rf"(?P<op><=|>=|<|>|\b(?:at\s+most|at\s+least|no\s+more\s+than|no\s+less\s+than|"
    rf"below|under|less\s+than|lower\s+than|above|over|greater\s+than|more\s+than)\b)"
    rf"\s*(?P<value>{_NUMBER})",
    re.I,
)
_BOUND_JOIN = re.compile(
    r"\s*(?:and|but|while|as\s+well\s+as|&|,(?:\s*(?:and|but))?)\s*(?:also\s+)?"
    r"(?:(?:the\s+)?(?:values|numbers|results|it|they)\s+)?(?:(?:are|is)\s+)?",
    re.I,
)
_ICON_CONDITION = re.compile(r"\b(?:only\s+(?:for|where|when|if)|when|whenever|if)\s+(.+)", re.I)


def _numeric_icon_condition(tail: str, operands: list[str]) -> bool:
    """Every part of a restricted icon predicate must be a numeric bound."""
    # A trailing visual location is not another data condition. Quoted names stay
    # inert here, including a table caption containing words such as "Only for".
    tail = re.sub(
        r"\s+(?:on|in)\s+(?:the\s+|this\s+|that\s+)?"
        r"(?:\x01\d+\x01(?:\s+(?:table|matrix|visual|column|page))?|"
        r"(?:table|matrix|visual|column|page)(?:\s+\x01\d+\x01)?)"
        r"\s*[.!?]*$",
        "",
        tail,
        flags=re.I,
    )
    for operand in sorted(operands, key=len, reverse=True):
        tail = re.sub(rf"^(?:the\s+)?{re.escape(operand)}\s+(?:is\s+)?", "", tail, flags=re.I)
    tail = re.sub(
        r"^(?:(?:the\s+)?(?:values?|numbers?|results?|they|it)\s+)?"
        r"(?:(?:are|is)\s+)?",
        "",
        tail,
        flags=re.I,
    ).rstrip(" .!?")
    bound = _BOUND.match(tail)
    if bound is None:
        return (
            re.fullmatch(rf"between\s+{_NUMBER}\s+and\s+{_NUMBER}|negative", tail, re.I) is not None
        )
    while bound.end() < len(tail):
        join = _BOUND_JOIN.match(tail, bound.end())
        following = _BOUND.match(tail, join.end()) if join is not None else None
        if following is None:
            # Explicit per-arrow rules may follow an outer range; they are checked
            # by _rules_preserve_range below, rather than mistaken for a category.
            rules = re.sub(r"^[,;\s]*(?:with\s+)?", "", tail[bound.end() :], flags=re.I)
            if re.fullmatch(
                rf"(?:cutoffs?|thresholds?)\s+{_NUMBER}(?:\s*(?:,|and)\s*{_NUMBER})+",
                rules,
                re.I,
            ):
                return True
            parts = re.split(r",\s*(?:and\s+)?|\s+and\s+", rules, flags=re.I)
            return all(
                re.fullmatch(
                    rf"(?:down|up|right|left)\s+(?:(?:below|under|above|over)\s+{_NUMBER}|"
                    rf"from\s+{_NUMBER}\s+to\s+{_NUMBER})",
                    part,
                    re.I,
                )
                is not None
                for part in parts
            )
        bound = following
    return True


def _range(request: str) -> _Range | None:
    # A restriction on all values is distinct from several explicit per-icon conditions.
    bounds = list(_BOUND.finditer(request))
    outer = next(
        (
            i
            for i, bound in enumerate(bounds)
            if re.search(
                r"\b(?:only|values|numbers|results|it\s+is)\s+(?:(?:are|is)\s+)?$",
                request[: bound.start()],
                re.I,
            )
        ),
        None,
    )
    if outer is not None:
        # Retain both sides of "only below 0 and above -5", but do not absorb a
        # later per-icon condition such as "with red below -20" into that range.
        linked = [bounds[outer]]
        for bound in bounds[outer + 1 :]:
            if not _BOUND_JOIN.fullmatch(request[linked[-1].end() : bound.start()]):
                break
            linked.append(bound)
        bounds = linked
    lower: list[tuple[float, bool]] = []
    upper: list[tuple[float, bool]] = []
    for bound in bounds:
        op = re.sub(r"\s+", " ", bound.group("op").casefold())
        inclusive = op in {"<=", ">=", "at most", "at least", "no more than", "no less than"}
        below = op in {
            "<",
            "<=",
            "at most",
            "no more than",
            "below",
            "under",
            "less than",
            "lower than",
        }
        (upper if below else lower).append((_number(bound.group("value")), inclusive))
    if lower or upper:
        minimum = max(value for value, _ in lower) if lower else None
        maximum = min(value for value, _ in upper) if upper else None
        return _Range(
            minimum,
            maximum,
            bool(lower) and all(inclusive for value, inclusive in lower if value == minimum),
            bool(upper) and all(inclusive for value, inclusive in upper if value == maximum),
        )
    if re.search(
        r"\bnegative\s+(?:values|numbers|results)|\bvalues\s+(?:are\s+)?negative\b", request, re.I
    ):
        return _Range(maximum=0)
    return None


def _rules_preserve_range(
    request: str,
    rules: Any,
    wanted: _Range,
    count: int | None,
    expected_icons: tuple[str, ...] | None,
) -> bool:
    if not isinstance(rules, list) or not rules or (count is not None and len(rules) != count):
        return False
    numeric = {_number(m.group()) for m in re.finditer(_NUMBER, request, re.I)}
    intervals: list[tuple[float, float, bool, bool]] = []
    for rule in rules:
        if (
            not isinstance(rule, Mapping)
            or not rule.get("icon")
            or rule.get("unit", "number") != "number"
        ):
            return False
        if rule.get("equals") is not None or rule.get("blank"):
            return False
        low, high = rule.get("min"), rule.get("max")
        if any(
            v is not None
            and (isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(v))
            for v in (low, high)
        ):
            return False
        if any(v is not None and float(v) not in numeric for v in (low, high)):
            return False  # a model cannot invent the cutoffs between several arrows
        lo, hi = (
            float(low) if low is not None else -math.inf,
            float(high) if high is not None else math.inf,
        )
        if lo >= hi:
            return False
        intervals.append(
            (lo, hi, bool(rule.get("min_inclusive", True)), bool(rule.get("max_inclusive", False)))
        )
    intervals.sort()
    if expected_icons is not None:
        by_interval = sorted(
            rules, key=lambda r: float(r["min"]) if r.get("min") is not None else -math.inf
        )
        if tuple(r["icon"] for r in by_interval) != expected_icons:
            return False
    minimum = wanted.minimum if wanted.minimum is not None else -math.inf
    maximum = wanted.maximum if wanted.maximum is not None else math.inf
    if intervals[0][0] != minimum or intervals[-1][1] != maximum:
        return False
    if math.isfinite(minimum) and intervals[0][2] != wanted.min_inclusive:
        return False
    if math.isfinite(maximum) and intervals[-1][3] != wanted.max_inclusive:
        return False
    return all(
        left[1] == right[0] and left[3] != right[2]
        for left, right in zip(intervals, intervals[1:], strict=False)
    )


def _icons(context: EditContext, request: str, values: dict[str, Any]) -> SemanticConstraint:
    if values.get("style") != "icons":
        return SemanticConstraint(values)
    # A numeric preset cannot encode a categorical restriction such as "only for
    # Online". Ask before both deterministic and model proposals can drop that scope.
    operands = []
    field = values.get("based_on") or values.get("field")
    if isinstance(field, str) and field:
        operands.append(field)
    elif isinstance(field, Mapping) and isinstance(field.get("name"), str):
        operands.append(field["name"])
    outside_quotes, quotes = mask_quotes(request)
    for index, literal in enumerate(quotes):
        name = literal[1:-1]
        if any(name.casefold() == operand.casefold() for operand in operands):
            marker = f"\x01{index}\x01"
            subject = re.search(re.escape(marker) + r"\s+(?:is\s+)?", outside_quotes, re.I)
            if subject is not None and _BOUND.match(outside_quotes, subject.end()) is not None:
                outside_quotes = outside_quotes.replace(marker, name)
    condition = _ICON_CONDITION.search(outside_quotes)
    first_bound = _BOUND.search(outside_quotes)
    tail = (
        condition.group(1)
        if condition is not None
        else (outside_quotes[first_bound.start() :] if first_bound is not None else None)
    )
    foreign_operand = False
    if first_bound is not None:
        prefix = outside_quotes[: first_bound.start()]
        foreign_operand = any(
            name.casefold() not in {operand.casefold() for operand in operands}
            and re.search(rf"(?<!\w){re.escape(name)}\s+(?:is\s+)?$", prefix, re.I)
            for name in [
                *(measure.name for measure in context.measures),
                *(name for columns in context.tables.values() for name in columns),
            ]
        )
    if foreign_operand or (tail is not None and not _numeric_icon_condition(tail, operands)):
        return _ask(
            values,
            request,
            "Which field and condition should restrict the icons? Provide explicit "
            "numeric bounds and the icon for each range; the condition will not be "
            "replaced by a whole-range preset.",
        )
    arrow = bool(re.search(r"\barrows?\b", request, re.I))
    count_match = _ARROW_COUNT.search(request)
    count = _COUNT_NAMES[count_match.group(1).casefold()] if count_match else None
    icon_set = str(values.get("icon_set") or "")
    compact = re.sub(r"\W", "", request).casefold()
    named = bool(icon_set and re.sub(r"\W", "", icon_set).casefold() in compact)
    named_arrow_icons = None
    if named and icon_set.casefold().endswith("arrows"):
        from .report_edit_conditional import ICON_SETS

        named_arrow_icons = ICON_SETS.get(icon_set)
        if named_arrow_icons is None:
            return _ask(values, request, "Which supported arrow icon set should be used?")
        count = len(named_arrow_icons)
    wanted = _range(request)
    if wanted is not None:
        expected_icons = (
            named_arrow_icons or _COLORED_ARROW_ICONS.get(count)
            if count is not None and (named or re.search(r"\bcolou?red\b", request, re.I))
            else None
        )
        if _rules_preserve_range(request, values.get("rules"), wanted, count, expected_icons):
            values.pop("icon_set", None)  # explicit rules and a preset cannot both apply
            return SemanticConstraint(values)
        conditions = []
        if wanted.minimum is not None:
            conditions.append(
                f"{'at least' if wanted.min_inclusive else 'above'} {wanted.minimum:g}"
            )
        if wanted.maximum is not None:
            conditions.append(
                f"{'at most' if wanted.max_inclusive else 'below'} {wanted.maximum:g}"
            )
        condition_text = " and ".join(conditions)
        return _ask(
            values,
            request,
            f"The requested condition ({condition_text}) must be kept; an icon preset "
            "covers the whole range. Which icon should each requested numeric range get? "
            "For several arrows, give the cutoffs inside that range.",
        )
    if re.search(
        r"\b(?:below|under|above|over|less\s+than|greater\s+than)\s+(?:the\s+)?"
        r"(?:target|threshold|average|mean|median|limit)\b|\bbetween\b",
        request,
        re.I,
    ):
        return _ask(
            values,
            request,
            "What numeric bounds should the icon condition use? "
            "Name the target or thresholds and the icon for each range; the condition "
            "will not be replaced by a whole-range preset.",
        )
    if arrow and count is None and not named and not values.get("rules"):
        from .report_edit_conditional import ICON_SETS

        sets = [name for name in ICON_SETS if name.endswith("Arrows")]
        if re.search(r"\bcolou?red\b", request, re.I):
            sets = [name for name in sets if "Colored" in name]
        elif re.search(r"\bgr[ae]y(?:ed)?\b", request, re.I):
            return _ask(
                values,
                request,
                "Which supported gray arrow set should be used? "
                "Name the set or specify its thresholds.",
            )
        categories = [
            {3: "three", 4: "four", 5: "five"}[number]
            for number in sorted({len(ICON_SETS[name]) for name in sets})
        ]
        count_words = ", ".join(categories[:-1]) + f", or {categories[-1]}"
        return SemanticConstraint(
            values,
            Clause(
                request,
                ParseKind.CLARIFY,
                question=f"How many arrow categories should the icons use: {count_words}? "
                "Name the set or specify its thresholds.",
            ),
            choices=tuple(Choice(name, fill={"icon_set": name}) for name in sets),
        )
    if count is not None and re.search(r"\bcolou?red\b", request, re.I):
        values["icon_set"] = _COLORED_ARROWS[count]
    return SemanticConstraint(values)


def constrain_edit_request(
    context: EditContext,
    request: str,
    card_or_op: ToolCard | str,
    values: Mapping[str, Any],
) -> SemanticConstraint:
    """Correct grounded slots, or ask before a default can change the request's meaning.

    ``values`` may be filled card slots or an operation dict. Neither the original mapping
    nor the context is mutated. A clarification contains no executable operations.
    """
    filled = copy.deepcopy(dict(values))
    op = card_or_op.op if isinstance(card_or_op, ToolCard) else card_or_op
    if op == "set_visual_calculation":
        return _calculation(context, request, filled)
    if op == "set_interaction":
        return _interaction(context, request, filled)
    if op == "add_button":
        return _button(context, request, filled)
    if op == "set_conditional_format":
        return _icons(context, request, filled)
    return SemanticConstraint(filled)
