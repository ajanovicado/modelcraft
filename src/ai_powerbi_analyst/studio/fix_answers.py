"""A fix for a measure that repeats one condition, with its effect on the report shown.

DEV-05 (2026-09-27 battery): the Others bar of 'TopN/BottomN/ MTD Sales' filtered
[Rank MTD Desc] > _N && [Rank MTD Desc] > _N, so it also counted the bottom N. Told about
the duplicate, the model still rewrote it as [Rank MTD Asc] <= _N and remapped the SWITCH,
which would have broken the chart. When the report has the condition's sibling measure
([Rank MTD Asc] beside [Rank MTD Desc]), the fix is that one swap; Studio's DAX engine then
recomputes the visual that shows the measure, in the page's saved state, before and after.
"""

from __future__ import annotations

import re

from .dax_engine import DaxEngineError, Model, run_query
from .dax_format import format_number
from .live_report import LiveReportRequest, compile_page
from .report_import import ImportedReport

_SIBLINGS = (
    ("asc", "desc"),
    ("top", "bottom"),
    ("min", "max"),
    ("first", "last"),
    ("high", "low"),
    ("best", "worst"),
)


def _swap(word: str, target: str) -> str:
    return target.upper() if word.isupper() else target.title() if word[0].isupper() else target


def sibling_fix(expression: str, repeated: str, measures: set[str]) -> tuple[str, str] | None:
    """(find, replace): the repeated condition's second copy with its sibling measure."""
    parts = re.split(r"\s*(&&|\|\|)\s*", repeated, maxsplit=1)
    if len(parts) != 3:
        return None
    first, operator, second = parts
    by_key = {m.casefold(): m for m in measures}
    found: list[str] = []
    for ref in dict.fromkeys(re.findall(r"\[([^\]]+)\]", second)):
        for a, b in _SIBLINGS:
            for word, other in ((a, b), (b, a)):
                pattern = re.compile(rf"\b{word}\b", re.I)
                if pattern.search(ref):

                    def swap_match(match: re.Match[str], target: str = other) -> str:
                        return _swap(match.group(0), target)

                    sibling = pattern.sub(swap_match, ref)
                    if sibling.casefold() in by_key:
                        found.append(second.replace(f"[{ref}]", f"[{by_key[sibling.casefold()]}]"))
    if len(set(found)) != 1:
        return None  # no sibling, or several: the model decides
    return repeated, f"{first} {operator} {found[0]}"


def _measure_named(report: ImportedReport, expression: str) -> str | None:
    wanted = " ".join(expression.split())
    for measure in report.measures:
        if " ".join(str(measure.get("Expression", "")).split()) == wanted:
            return str(measure["Name"])
    return None


def fix_effect(report: ImportedReport, model: Model, expression: str, fixed: str) -> str | None:
    """What the visual showing this measure displays before and after the fix."""
    name = _measure_named(report, expression)
    if name is None:
        return None
    for page in report.pages:
        for visual in page.visuals:
            if not any(b.get("name") == name for b in visual.bindings):
                continue
            try:
                metadata, query = compile_page(
                    report, page.id, LiveReportRequest(), only={visual.id}
                )
                if not query:
                    continue
                table = next(b.get("table", "") for b in visual.bindings if b.get("name") == name)
                define = (
                    f"DEFINE MEASURE '{str(table).replace(chr(39), chr(39) * 2)}'"
                    f"[{name.replace(']', ']]')}] = {fixed}\n"
                )
                before = run_query(model, query, time_budget=60).rows
                after = run_query(model, define + query, time_budget=60).rows
            except (DaxEngineError, ValueError, LookupError):
                continue
            columns = metadata[0]["columns"] if metadata else []
            index = next((i for i, c in enumerate(columns) if c.get("name") == name), None)
            if index is None or len(before) != len(after):
                continue
            label_index = next(
                (i for i, c in enumerate(columns) if c.get("kind") == "column"), None
            )
            # Result columns: [Visual], then C/K/F (value, kind, formatted) per field.
            lines = []
            total_before = total_after = 0.0
            for old, new in zip(before, after, strict=True):
                old_value = _number(old[1 + 3 * index])
                new_value = _number(new[1 + 3 * index])
                label = old[1 + 3 * label_index] if label_index is not None else visual.title
                total_before += old_value or 0
                total_after += new_value or 0
                was = "" if old_value == new_value else f" (was {_amount(old_value)})"
                lines.append(f"{label}: {_amount(new_value)}{was}")
            return (
                f"In '{visual.title}' on page {page.title}, with the page's saved filters: "
                + "; ".join(lines)
                + f". Together {_amount(total_after)} (before: {_amount(total_before)})."
            )
    return None


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def _amount(value: float | None) -> str:
    return "blank" if value is None else format_number(value, "#,##0.00")
