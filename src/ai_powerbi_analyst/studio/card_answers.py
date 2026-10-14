"""Answers about an imported report's cards: the figures computed, the meaning described.

MAN-01 and MAN-08 asked what a card shows. The model guessed ("likely counts employees
over 100K MTD", "employees whose headcount increased") and gave no number, because the
card's measure needs time intelligence the old evaluator lacked. Now the card's figures
come from the same query that draws it, run by Studio's DAX engine (19 against a target
of 20; 46.76% against 55%, ✔ 85.0%), and what it measures comes from its formula
(``dax_describe``). MAN-10 asked a what-if ("change the 100K target to 120K, how many
would make it"): the card's formula is recalculated with the new threshold.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .dax_describe import describe, referenced_measures
from .dax_engine import DaxEngineError, Model, _related_path
from .dax_format import format_number
from .dax_syntax import (
    Binary,
    Call,
    ColRef,
    Name,
    Node,
    Num,
    Str,
    VarBlock,
    parse_expression,
    replace_nodes,
    to_dax,
)
from .live_report import KINDS, LiveReportRequest, render_page
from .report_engine import LocalReportEngine
from .report_import import ImportedReport, ImportedVisual


@dataclass(frozen=True)
class CardRef:
    page_id: str
    page_title: str
    visual_id: str
    label: str
    measure: str
    names: tuple[str, ...]


_STOP_WORDS = (
    "the a an of on in at this that these card cards kpi tile number value values "
    "what whats does do did is are was were mean means meaning show shows showing "
    "shown right now and we our how much many with to for it its it's me tell about "
    "explain why which who there here i you please can could would should be by from "
    "as currently current"
)
_STOP = frozenset(_STOP_WORDS.split())
# "Write DAX that shows total sales" is no card question: only these words name one.
_CARD_WORD = re.compile(r"\b(?:cards?|kpis?|tiles?|callouts?)\b", re.I)
_CARD_TALK = re.compile(
    r"\b(?:on target|on track|hit(?:ting)? (?:the |its )?target|showing|mean(?:s|ing)?|"
    r"stand for|red|green|tick|cross)\b|✔|✘",
    re.I,
)
_LIST_ASK = re.compile(r"^\s*(?:which|who|list|name|rank|top|bottom|show me (?:the|all))\b", re.I)
_QUOTED = re.compile(r"['\"‘’“”]([^'\"‘’“”]{3,120})['\"‘’“”]")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00a0]+")
_AMOUNT = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s*([kKmM]\b)?")
# A what-if names two amounts: "change the 100K target to 120K", "what if the 100K bar
# were 80K instead", "raise it from 100K to 120K".
_WHAT_IF_CUE = re.compile(
    r"\b(?:to|instead|were|was|if|raise[ds]?|lower(?:ed)?|change[ds]?|set|increase[ds]?|"
    r"decrease[ds]?|move[ds]?|bump(?:ed)?|drop(?:ped)?|rather than)\b",
    re.I,
)


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9%]+", text.casefold()))


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9%]+", text.casefold())) - _STOP


def _literal(value: object) -> str | None:
    if isinstance(value, dict):
        literal = value.get("expr", {}).get("Literal", {}).get("Value")
        if isinstance(literal, str) and len(literal) > 1 and literal[0] == literal[-1] == "'":
            return literal[1:-1]
    return None


def _card_label(visual: ImportedVisual) -> str:
    objects = visual.definition.get("visual", {}).get("objects", {})
    for entry in objects.get("label", []):
        text = _literal(entry.get("properties", {}).get("text"))
        if text:
            return text.strip()
    containers = visual.definition.get("visual", {}).get("visualContainerObjects", {})
    for entry in containers.get("title", []):
        text = _literal(entry.get("properties", {}).get("text"))
        if text:
            return text.strip()
    return visual.title


def _measures_named(definition: object) -> list[str]:
    """Every measure a visual definition names (values, reference labels, images)."""
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            measure = value.get("Measure")
            if isinstance(measure, dict) and isinstance(measure.get("Property"), str):
                found.append(measure["Property"])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(definition)
    return list(dict.fromkeys(found))


def cards(report: ImportedReport) -> list[CardRef]:
    found = []
    for page in report.pages:
        for visual in page.visuals:
            if KINDS.get(visual.visual_type) != "card":
                continue
            measures = [b["name"] for b in visual.bindings if b.get("kind") == "measure"]
            if not measures:
                continue
            label = _card_label(visual)
            names = tuple(
                dict.fromkeys([label, visual.title, *measures, *_measures_named(visual.definition)])
            )
            found.append(CardRef(page.id, page.title, visual.id, label, measures[0], names))
    return found


def find_card(message: str, report: ImportedReport, visual_id: str | None = None) -> CardRef | None:
    """The card a question is about, or None when it names none (or several) clearly."""
    candidates = cards(report)
    if visual_id:
        return next((c for c in candidates if c.visual_id == visual_id), None)
    quoted = {_norm(q) for q in _QUOTED.findall(message)}
    exact = [c for c in candidates if quoted & {_norm(n) for n in c.names}]
    if len({c.visual_id for c in exact}) == 1:
        return exact[0]
    explicit = _CARD_WORD.search(message) is not None
    if not explicit and not _CARD_TALK.search(message):
        return None
    if _LIST_ASK.search(message) and not explicit:
        return None
    asked = _words(message)
    overlaps = sorted(
        ((len(asked & (_words(c.label) | _words(c.measure))), c) for c in candidates),
        key=lambda item: item[0],
        reverse=True,
    )
    if not overlaps or overlaps[0][0] == 0:
        return None
    best, card = overlaps[0]
    if len(overlaps) > 1 and overlaps[1][0] == best:
        return None  # two cards fit equally: ask the model, not a guess
    numbered = any(
        ch.isdigit() for w in asked & (_words(card.label) | _words(card.measure)) for ch in w
    )
    if explicit or best >= 2 or numbered:
        return card
    return None


def _clean(text: object) -> str:
    return " ".join(_INVISIBLE.sub(" ", str(text)).split())


def _plain_number(value: float) -> str:
    if float(value).is_integer():
        return f"{int(value):,}"
    return format_number(value, "#,##0.00")


def _shown(value: object, formatted: object, like: str | None) -> str:
    if formatted not in (None, ""):
        return _clean(formatted)
    if isinstance(value, int | float) and not isinstance(value, bool):
        if like and "%" in like and abs(value) <= 10:
            return f"{value:.2%}"
        return _plain_number(value)
    return _clean(value) if value is not None else "blank"


def _resolve(node: Node) -> tuple[Node, VarBlock | None]:
    """The RETURN expression with variable names resolved one step."""
    if isinstance(node, VarBlock):
        body = node.body
        if isinstance(body, Name):
            found = dict((n.casefold(), v) for n, v in node.variables).get(body.name.casefold())
            if found is not None:
                return found, node
        return body, node
    return node, None


def _in_block(block: VarBlock | None, expression: Node) -> str:
    if block is None:
        return to_dax(expression)
    return to_dax(VarBlock(block.variables, expression))


def _ratio_parts(expression: str) -> tuple[str, str] | None:
    """DAX for the numerator and denominator when the measure is a ratio."""
    try:
        node, block = _resolve(parse_expression(expression))
    except ValueError:
        return None
    if isinstance(node, Call) and node.name == "DIVIDE" and len(node.args) >= 2:
        top, bottom = node.args[0], node.args[1]
    elif isinstance(node, Binary) and node.op == "/":
        top, bottom = node.left, node.right
    else:
        return None
    return _in_block(block, top), _in_block(block, bottom)


def _tick_rule(expression: str) -> str | None:
    """ "✔ at 80% of the target or more" from IF(value >= 0.8, "✔ ", "✘ ")."""
    try:
        node = parse_expression(expression)
    except ValueError:
        return None
    rule: list[str] = []

    def walk(n: Node) -> None:
        if isinstance(n, Call) and n.name == "IF" and len(n.args) == 3:
            test, yes = n.args[0], n.args[1]
            if (
                isinstance(test, Binary)
                and test.op in {">=", ">"}
                and isinstance(test.right, Num)
                and isinstance(yes, Str)
                and re.search(r"[✔✓]|green", yes.value, re.I)
            ):
                share = float(test.right.value)
                amount = f"{share:.0%}" if share <= 5 else _plain_number(share)
                rule.append(
                    f"it is marked {yes.value.strip()} at {amount} of its target or more"
                    if test.op == ">="
                    else f"it is marked {yes.value.strip()} above {amount} of its target"
                )
        if isinstance(n, Call):
            for a in n.args:
                walk(a)
        elif isinstance(n, VarBlock):
            for _, v in n.variables:
                walk(v)
            walk(n.body)

    walk(node)
    return rule[0] if rule else None


def _definitions(measure: str, measures: dict[str, str], depth: int = 3) -> list[str]:
    """Bullets describing the measure's own dependencies, nearest first."""
    lines, seen, frontier = [], {measure.casefold()}, [measure]
    names = set(measures)
    for _ in range(depth):
        following = []
        for current in frontier:
            for name in referenced_measures(measures.get(current, ""), measures):
                if name.casefold() in seen:
                    continue
                seen.add(name.casefold())
                text = describe(measures[name], names)
                if text is None:
                    continue
                if re.fullmatch(r"[\d.,]+", text):
                    continue  # a constant target; the card shows it
                lines.append(f"- **{name}**: {text}")
                following.append(name)
        frontier = following
    return lines


def _chain(model: Model, measures: dict[str, str], measure: str) -> str | None:
    """ "FactSales → DimStore → DimEmployee" when the counted table is not the fact table."""
    text = " ".join(measures.get(m, "") for m in [measure, *_all_references(measure, measures)])
    counted = re.findall(r"(?:ALL|ALLSELECTED|VALUES)\s*\(\s*'?([\w ]+?)'?\s*\[", text)
    summed = re.findall(r"(?:SUM|SUMX|COUNTROWS|AVERAGE)\s*\(\s*'?([\w ]+?)'?\s*[\[,)]", text)
    if not counted or not summed:
        return None
    start, end = summed[0].strip().casefold(), counted[0].strip().casefold()
    if start == end or start not in model.tables or end not in model.tables:
        return None
    path = _related_path(model, start, end)
    if not path:
        return None
    names = [model.tables[start].name] + [model.tables[rel.one].name for rel in path]
    return " → ".join(names)


def _all_references(measure: str, measures: dict[str, str]) -> list[str]:
    seen: list[str] = []
    frontier = [measure]
    while frontier:
        current = frontier.pop()
        for name in referenced_measures(measures.get(current, ""), measures):
            if name not in seen:
                seen.append(name)
                frontier.append(name)
    return seen


def _period(measures: dict[str, str], measure: str, end: object, last: object) -> str:
    related = [measure, *_all_references(measure, measures)]
    text = " ".join(measures.get(m, "") for m in related).upper()
    if not isinstance(end, datetime):
        return ""
    through = f"{end.day} {end:%b %Y}"
    if "DATESMTD" in text or "TOTALMTD" in text:
        period = f" for {end:%B %Y}, month to date (through {through})"
    elif "DATESQTD" in text or "TOTALQTD" in text:
        period = f" for Q{(end.month - 1) // 3 + 1} {end.year}, quarter to date (through {through})"
    elif "DATESYTD" in text or "TOTALYTD" in text:
        period = f" for {end.year}, year to date (through {through})"
    else:
        return ""
    if isinstance(last, datetime) and last.date() == end.date():
        period += ", the last date in the data"
    return period


def _date_extras(model: Model) -> list[str]:
    if not model.date_keys:
        return []
    table, column = sorted(model.date_keys)[0]
    date_ref = f"'{model.tables[table].name}'[{model.tables[table].columns[column].name}]"
    fact = next(
        (
            (rel.many, rel.many_column)
            for rel in model.relationships
            if (rel.one, rel.one_column) == (table, column) and rel.active
        ),
        None,
    )
    extras = [f"MAX({date_ref})"]
    if fact is not None:
        many = model.tables[fact[0]]
        extras.append(
            f"CALCULATE(MAX('{many.name}'[{many.columns[fact[1]].name}]), REMOVEFILTERS())"
        )
    return extras


# Public names of the card pieces number_explain reuses: the period sentence of a time-
# intelligence measure, the probes it needs (the period's end, the last date in the data) and a
# card's own label text.
card_period = _period
period_probes = _date_extras
card_label = _card_label


Renderer = Callable[..., dict[str, Any]]


def card_answer(
    message: str,
    report: ImportedReport,
    model: Model,
    *,
    visual_id: str | None = None,
    renderer: Renderer = render_page,
) -> str | None:
    """What a card shows and means, computed; None when the question names no card."""
    card = find_card(message, report, visual_id)
    if card is None:
        return None
    measures = {m["Name"]: m["Expression"] for m in report.measures if m.get("Expression")}
    ratio = _ratio_parts(measures.get(card.measure, ""))
    extras = _date_extras(model)
    extras += list(ratio) if ratio else []
    try:
        rendered = renderer(
            LocalReportEngine(model),
            report,
            card.page_id,
            LiveReportRequest(),
            only={card.visual_id},
            extra={card.visual_id: extras},
        )
    except (DaxEngineError, ValueError, LookupError, TypeError):
        return None
    visual = next((v for v in rendered["visuals"] if v["id"] == card.visual_id), None)
    if visual is None or visual.get("error") or not visual.get("rows"):
        return None
    values = visual["rows"][0]
    formatted = (visual.get("formatted_rows") or [[None] * len(values)])[0]
    columns = visual["columns"]
    main_format = columns[0].get("format") if columns else None
    extra_values = visual.get("extra") or []
    end = extra_values[0] if extra_values else None
    last = extra_values[1] if len(extra_values) > 1 else None
    lines = [
        f"**{card.label}** shows **{_shown(values[0], formatted[0], main_format)}**"
        f"{_period(measures, card.measure, end, last)}."
    ]
    if ratio and len(extra_values) >= len(extras):
        top, bottom = extra_values[-2], extra_values[-1]
        if isinstance(top, int | float) and isinstance(bottom, int | float) and bottom:
            lines.append(f"That is {_plain_number(top)} out of {_plain_number(bottom)}.")
    formatting = visual.get("formatting") or {}
    for reference in formatting.get("references", []):
        index = reference.get("index")
        if index is None or index >= len(values):
            continue
        name = _clean(reference.get("label", "")).rstrip(":") or columns[index].get("label")
        text = f"- {name}: {_shown(values[index], formatted[index], main_format)}"
        detail_index = reference.get("detailIndex")
        if detail_index is not None and detail_index < len(values) and values[detail_index]:
            text += f" ({_clean(values[detail_index])})"
            rule = _tick_rule(measures.get(columns[detail_index].get("name", ""), ""))
            if rule:
                text += f"; {rule}"
        lines.append(text)
    meaning = describe(measures.get(card.measure, ""), set(measures))
    lines.append("")
    if meaning:
        lines.append(f"**What it measures** ({card.measure}): {meaning}.")
    else:
        formula = " ".join(measures.get(card.measure, "").split())[:400]
        lines.append(f"**Formula** ({card.measure}): `{formula}`")
    lines.extend(_definitions(card.measure, measures))
    chain = _chain(model, measures, card.measure)
    if chain:
        lines.append(f"The figures reach {chain.split(' → ')[-1]} through {chain}.")
    lines.append("")
    lines.append(
        "_Calculated by Studio's DAX engine on the imported data, with the page's saved "
        "filters: the same figures Power BI shows on this card._"
    )
    return "\n".join(lines)


def _amount(number: str, suffix: str | None) -> float:
    value = float(number.replace(",", ""))
    return value * {"k": 1_000, "m": 1_000_000}.get((suffix or "").casefold(), 1)


def what_if_answer(message: str, report: ImportedReport, model: Model) -> str | None:
    """A card recalculated with one threshold changed ("the 100K target to 120K")."""
    if not _WHAT_IF_CUE.search(message):
        return None
    amounts = list(
        dict.fromkeys(_amount(m.group(1), m.group(2)) for m in _AMOUNT.finditer(message))
    )
    if len(amounts) != 2:
        return None
    measures = {m["Name"]: m["Expression"] for m in report.measures if m.get("Expression")}
    candidates = []
    for old, new in (tuple(amounts), tuple(reversed(amounts))):
        for card in cards(report):
            try:
                node = parse_expression(measures.get(card.measure, ""))
            except ValueError:
                continue
            hits: list[Node] = []

            def mark(
                n: Node, hits: list[Node] = hits, old: float = old, new: float = new
            ) -> Node | None:
                if isinstance(n, Num) and float(n.value) == old:
                    hits.append(n)
                    return Num(int(new) if float(new).is_integer() else new)
                return None

            changed = replace_nodes(node, mark)
            if hits:
                candidates.append((card, node, changed, old, new))
    named = find_card(message, report)
    if named is not None:
        candidates = [c for c in candidates if c[0].visual_id == named.visual_id] or candidates
    if len(candidates) != 1:
        return None
    card, original, changed, old, new = candidates[0]
    extras = [to_dax(changed), to_dax(original), *_date_extras(model)]
    dropped = _dropped(original, changed, model, raised=new > old)
    extras += [dropped] if dropped else []
    try:
        rendered = render_page(
            LocalReportEngine(model),
            report,
            card.page_id,
            LiveReportRequest(),
            only={card.visual_id},
            extra={card.visual_id: extras},
        )
    except (DaxEngineError, ValueError, LookupError, TypeError):
        return None
    visual = next((v for v in rendered["visuals"] if v["id"] == card.visual_id), None)
    if visual is None or not visual.get("extra"):
        return None
    values = visual["extra"]
    after, before = values[0], values[1]
    end = values[2] if len(values) > 2 else None
    last = values[3] if len(values) > 3 else None
    main_format = visual["columns"][0].get("format") if visual.get("columns") else None
    period = _period(measures, card.measure, end, last)
    text = (
        f"With {_plain_number(new)} instead of {_plain_number(old)} in **{card.measure}**, "
        f"**{card.label}** would show **{_shown(after, None, main_format)}**{period}, "
        f"instead of {_shown(before, None, main_format)}."
    )
    if dropped and len(values) > 4 and values[-1]:
        verb = "Dropping out" if new > old else "Newly included"
        text += f" {verb}: {values[-1]}."
    text += (
        f"\n\nThe report has not changed. To change the card, edit {card.measure} in Power BI "
        f"Desktop and replace {_plain_number(old).replace(',', '')} with "
        f"{_plain_number(new).replace(',', '')}."
    )
    return text


def _dropped(original: Node, changed: Node, model: Model, *, raised: bool) -> str | None:
    """Names that move across the threshold, for COUNTX/COUNTROWS(FILTER(ALL(T[K]), ...))."""

    def filtered(node: Node) -> Call | None:
        node, _ = _resolve(node)
        if isinstance(node, Call) and node.name in {"COUNTX", "COUNTROWS"} and node.args:
            inner = node.args[0]
            if isinstance(inner, Call) and inner.name == "FILTER":
                return inner
        return None

    before, after = filtered(original), filtered(changed)
    if before is None or after is None:
        return None
    domain = before.args[0]
    if not (
        isinstance(domain, Call)
        and domain.name in {"ALL", "VALUES"}
        and len(domain.args) == 1
        and isinstance(domain.args[0], ColRef)
        and domain.args[0].table
    ):
        return None
    table_key = domain.args[0].table.casefold()
    table = model.tables.get(table_key)
    if table is None:
        return None
    label_columns = [
        c.name for c in table.columns.values() if c.kind == "string" and "name" in c.name.casefold()
    ][:2] or [domain.args[0].column]
    label = ' & " " & '.join(f"SELECTEDVALUE('{table.name}'[{column}])" for column in label_columns)
    first, second = (before, after) if raised else (after, before)
    return f'CONCATENATEX(EXCEPT({to_dax(first)}, {to_dax(second)}), CALCULATE({label}), ", ")'


def _svg_summary(text: str) -> str:
    """ "bar widths 260, 221; gradient #C6E3FF → #118DFF; dashed line at x 208"."""
    widths = re.findall(r"<rect[^>]*?width='(\d+(?:\.\d+)?)'", text)
    colors = re.findall(r"stop-color:\s*(#[0-9A-Fa-f]{3,8})", text)
    lines = re.findall(r"<line[^>]*?x1='(\d+(?:\.\d+)?)'", text)
    parts = []
    if widths:
        parts.append("bar widths " + ", ".join(widths))
    if colors:
        parts.append("gradient " + " → ".join(colors))
    if lines:
        parts.append("dashed line at x " + ", ".join(lines))
    return "an SVG image: " + "; ".join(parts) if parts else "an SVG image"


def _fact_value(value: object) -> str:
    if value is None:
        return "blank"
    if isinstance(value, float) and value.is_integer():
        return f"{int(value):,}"
    if isinstance(value, float):
        return f"{value:,.6g}" if abs(value) < 10 else format_number(value, "#,##0.00")
    text = _clean(value)
    if text.startswith("data:image/svg"):
        return _svg_summary(str(value))
    return text[:160]


def measure_facts(report: ImportedReport, model: Model, expression: str, goal: str) -> str:
    """What the measures an explanation is about show in the report's saved state.

    DEV-04 asked how wide and what colour the SVG bar is in the saved state: the model
    could not know and invented "530 px". Studio's DAX engine computes it (221 of 260,
    blue gradient), and the page's saved slicers (N = 6) are what DEV-01's ranks use.
    """
    measures = {str(m["Name"]): str(m.get("Expression", "")) for m in report.measures}
    wanted = " ".join(expression.split())
    names = [n for n, text in measures.items() if " ".join(text.split()) == wanted]
    named = {n.casefold(): n for n in measures}
    quoted = [q for q in re.findall(r"'([^']+)'", goal)]
    bracketed = re.findall(r"\[([^\[\]]+)\]", goal)
    for ref in [*quoted, *bracketed]:
        name = named.get(ref.casefold())
        if name and name not in names:
            names.append(name)
    names = names[:3]
    if not names:
        return ""
    facts: list[str] = []
    shown: dict[str, object] = {}
    slicers = ""
    for name in names:
        saved = _saved_value(report, model, name, want_slicers=not slicers)
        if saved is None:
            continue
        value, where, picked = saved
        slicers = slicers or picked
        facts.append(f"- [{name}]{where} = {_fact_value(value)}")
        shown[name] = value
    if not facts:
        return ""
    state = f" (saved slicers: {slicers})" if slicers else ""
    return (
        f"Computed on the imported data in the report's saved state{state}:\n"
        + "\n".join(
            facts
            + _formula_facts(model, names[0], measures, shown.get(names[0]))
            + _meaning_facts(model, names, measures, expression)
        )
        + "\n\n"
    )


_DIFFER = re.compile(
    r"\b(?:differ\w*|difference|compared?|comparison|versus|vs\.?|instead\s+of)\b", re.I
)


def measure_comparison(
    report: ImportedReport, model: Model, expression: str, goal: str
) -> str | None:
    """ "How is this measure different from [Employees Avg MTD Sales]?" from the formulas
    and the data, not the model.

    H21's model answer (twice, with the facts in its prompt) credited NOT ISBLANK([MTD
    Total Sales]) for the difference. Recomputed without each condition, that one changes
    nothing, and the other one, NOT ISBLANK(DimEmployee[EmployeeKey]), is the whole gap.
    """
    if not _DIFFER.search(goal):
        return None
    measures = {str(m["Name"]): str(m.get("Expression", "")) for m in report.measures}
    wanted = " ".join(expression.split())
    mine = [n for n, text in measures.items() if " ".join(text.split()) == wanted]
    named = {n.casefold(): n for n in measures}
    others = [
        named[ref.casefold()]
        for ref in [*re.findall(r"'([^']+)'", goal), *re.findall(r"\[([^\[\]]+)\]", goal)]
        if ref.casefold() in named and named[ref.casefold()] not in mine
    ]
    if len(mine) != 1 or len(set(others)) != 1:
        return None
    first, second = mine[0], others[0]
    known = set(measures)
    meanings = [describe(measures[n], known) for n in (first, second)]
    if any(m is None or len(m) > 300 for m in meanings):
        return None
    saved_first = _saved_value(report, model, first, want_slicers=False)
    saved_second = _saved_value(report, model, second, want_slicers=False)
    if saved_first is None or saved_second is None:
        return None
    from .dax_engine import Engine

    engine = Engine(model, time_budget=20)
    try:
        unfiltered = engine.evaluate_scalar("[" + first.replace("]", "]]") + "]")
        node = parse_expression(measures[first])
    except (DaxEngineError, ValueError, LookupError, TypeError):
        return None
    a, b = _fact_value(saved_first[0]), _fact_value(saved_second[0])
    lines = [
        f"**[{first}]** is {meanings[0]}.",
        f"**[{second}]** is {meanings[1]}.",
        "",
        f"In the report's saved state, [{first}] = {a} and [{second}] = {b}.",
    ]
    effects = _condition_effects(engine, node, unfiltered) if _fact_value(unfiltered) == a else []
    lines += effects
    rules = [
        line
        for line in _meaning_facts(model, [first, second], measures, expression)
        if line.startswith("- AVERAGEX") or "has a (Blank) member" in line
    ]
    lines += rules
    gap = [
        re.search(r"`([^`]+)`", line)[1]  # type: ignore[index]
        for line in effects
        if f"would be {b} instead of" in line
    ]
    if a == b:
        lines += ["", "Here both give the same result."]
    elif len(gap) == 1:
        lines += [
            "",
            f"So the whole difference is `{gap[0]}`: without it, [{first}] equals [{second}].",
        ]
    return "\n".join(lines)


def _saved_value(
    report: ImportedReport, model: Model, name: str, *, want_slicers: bool
) -> tuple[object, str, str] | None:
    """(value, where it was read, the page's saved slicers) of a measure in the saved state."""
    visual_page = next(
        (
            (page, visual)
            for page in report.pages
            for visual in page.visuals
            if name in _measures_named(visual.definition)
            or any(b.get("name") == name for b in visual.bindings)
        ),
        None,
    )
    ref = "[" + name.replace("]", "]]") + "]"
    value: object = None
    where = ""
    slicers = ""
    try:
        if visual_page is not None:
            page, visual = visual_page
            rendered = render_page(
                LocalReportEngine(model),
                report,
                page.id,
                LiveReportRequest(),
                only={visual.id},
                extra={visual.id: [ref]} if KINDS.get(visual.visual_type) == "card" else None,
            )
            item = rendered["visuals"][0] if rendered["visuals"] else {}
            if item.get("extra"):
                value = item["extra"][0]
            else:
                columns = [c.get("name") for c in item.get("columns", [])]
                if name in columns and item.get("rows"):
                    value = item["rows"][0][columns.index(name)]
                    if len(item["rows"]) > 1:
                        where = f" in the first row of '{visual.title}'"
            if want_slicers:
                full = render_page(LocalReportEngine(model), report, page.id, LiveReportRequest())
                picks = [
                    f"{v['title']} = {', '.join(_fact_value(x) for x in v['selected'])}"
                    for v in full["visuals"]
                    if v.get("kind") == "slicer" and v.get("selected")
                ]
                slicers = "; ".join(picks)
        if value is None:
            from .dax_engine import Engine

            value = Engine(model, time_budget=20).evaluate_scalar(ref)
    except (DaxEngineError, ValueError, LookupError, TypeError):
        return None
    return value, where, slicers


_COMPARISONS = {"=", "==", "<>", "<", "<=", ">", ">="}


def _meaning_facts(
    model: Model, names: list[str], measures: dict[str, str], expression: str
) -> list[str]:
    """What each measure computes, in fixed wording, and the DAX rules its result depends on.

    H21 asked how [Employees Avg MTD Sales - Adjusted] differs from [Employees Avg MTD
    Sales]; the model said the plain one "includes all employees regardless of whether
    they have sales data", but AVERAGEX leaves blank values out. The difference is the
    (Blank) agent: a DimStore row whose EmployeeKey is missing from DimEmployee. DEV-01's
    [Employee Name] had no sentence as a whole; its variables have one each.
    """
    from .dax_describe import describe_parts

    known = set(measures)
    facts: list[str] = []
    for name in names:
        text = measures.get(name, "")
        meaning = describe(text, known)
        if meaning is not None and len(meaning) <= 300:
            facts.append(f"- [{name}] is {meaning}.")
        elif name == names[0]:
            # One sentence for a whole SWITCH of RANKX results is unreadable; one per
            # variable is not.
            facts += [f"- {part}: {sentence}" for part, sentence in describe_parts(text, known)]
    helpers = [
        m for m in referenced_measures(measures.get(names[0], ""), measures) if m not in names
    ]
    for helper in helpers[:6]:
        meaning = describe(measures.get(helper, ""), known)
        if meaning is not None and len(meaning) <= 160:
            facts.append(f"- [{helper}] is {meaning}.")
    texts = " ".join([expression, *(measures.get(n, "") for n in names)])
    if re.search(r"\bAVERAGEX\s*\(", texts, re.I):
        facts.append(
            "- AVERAGEX leaves out rows where the averaged value is blank: they count "
            "neither in the sum nor in the number of rows."
        )
    for rel in model.relationships:
        one = model.tables[rel.one]
        key = one.columns[rel.one_column].name
        pattern = rf"ISBLANK\s*\(\s*'?{re.escape(one.name)}'?\s*\[{re.escape(key)}\]"
        unmatched = model.unmatched(rel)
        if rel.active and unmatched.any() and re.search(pattern, texts, re.I):
            many = model.tables[rel.many]
            column = many.columns[rel.many_column]
            missing = sorted(
                {column.value(int(i)) for i in unmatched.nonzero()[0]} - {None}, key=str
            )[:3]
            count = int(unmatched.sum())
            facts.append(
                f"- {one.name} has a (Blank) member: {count:,} {many.name} "
                f"row{'s' if count != 1 else ''} with {column.name} "
                f"{', '.join(str(m) for m in missing) or 'blank'}, missing from {one.name}. "
                f"ISBLANK({one.name}[{key}]) is TRUE for it; its sales still count in totals."
            )
    return facts


def _date_cutoff(engine: Any, node: Node, body: Binary, computed: dict[str, object]) -> list[str]:
    """ "FALSE for periods that start after 31 Dec 2009", for a first-visible-date test.

    DEV-03: told __FirstDateVisible <= __LastDateWithData, the model still said the
    measure is FALSE "when no date is selected"; the consequence is a fact to state.
    """
    if not (
        isinstance(node, VarBlock)
        and body.op in {"<=", "<"}
        and isinstance(body.left, Name)
        and isinstance(body.right, Name)
    ):
        return []
    first = dict(node.variables).get(body.left.name)
    cutoff = computed.get(body.right.name)
    if not (
        isinstance(first, Call)
        and first.name == "MIN"
        and len(first.args) == 1
        and isinstance(first.args[0], ColRef)
        and isinstance(cutoff, datetime)
    ):
        return []
    column = to_dax(first.args[0])
    try:
        later = engine.evaluate_scalar(
            f"COUNTROWS(FILTER(ALL({column}), {column} > "
            f"DATE({cutoff.year}, {cutoff.month}, {cutoff.day})))"
        )
    except (DaxEngineError, ValueError, LookupError, TypeError):
        return []
    edge = "on or after" if body.op == "<" else "after"
    return [
        f"- So it is FALSE for any period whose first date ({body.left.name}) is {edge} "
        f"{cutoff:%d %b %Y}, the last date with data: periods that have not happened yet. "
        f"{column} has {int(later or 0):,} dates after {cutoff:%d %b %Y}."
    ]


def _condition_effects(engine: Any, node: Node, value: object) -> list[str]:
    """What each condition of a FILTER changes: the result recomputed without it.

    H21: [Employees Avg MTD Sales - Adjusted] keeps NOT ISBLANK([MTD Total Sales]) and
    NOT ISBLANK(DimEmployee[EmployeeKey]). The first changes nothing (AVERAGEX skips
    blanks anyway); the second removes the (Blank) agent, 73,514.80 -> 73,778.96. The
    model credited the first.
    """
    filters: list[Call] = []

    def find(n: Node) -> None:
        if isinstance(n, Call):
            if n.name == "FILTER" and len(n.args) == 2:
                filters.append(n)
            for a in n.args:
                find(a)
        elif isinstance(n, Binary):
            find(n.left)
            find(n.right)
        elif isinstance(n, VarBlock):
            for _, v in n.variables:
                find(v)
            find(n.body)

    def conjuncts(n: Node) -> list[Node]:
        if isinstance(n, Binary) and n.op == "&&":
            return conjuncts(n.left) + conjuncts(n.right)
        if isinstance(n, Call) and n.name == "AND" and len(n.args) == 2:
            return conjuncts(n.args[0]) + conjuncts(n.args[1])
        return [n]

    find(node)
    if len(filters) > 2:
        return []
    facts: list[str] = []
    for target in filters:
        parts = conjuncts(target.args[1])
        if not 2 <= len(parts) <= 4:
            continue
        for index, part in enumerate(parts):
            kept = parts[:index] + parts[index + 1 :]
            condition = kept[0]
            for other in kept[1:]:
                condition = Binary("&&", condition, other)
            shorter = Call("FILTER", (target.args[0], condition))

            def replace_condition(
                candidate: Node,
                original: Call = target,
                replacement: Call = shorter,
            ) -> Node | None:
                return replacement if candidate is original else None

            changed = replace_nodes(node, replace_condition)
            try:
                without = engine.evaluate_scalar(to_dax(changed))
            except (DaxEngineError, ValueError, LookupError, TypeError):
                continue
            shown = to_dax(part)
            if _fact_value(without) == _fact_value(value):
                facts.append(
                    f"- Without `{shown}` the result is the same: it changes nothing here."
                )
            else:
                facts.append(
                    f"- Without `{shown}` the result would be {_fact_value(without)} instead of "
                    f"{_fact_value(value)}."
                )
    return facts


def _formula_facts(model: Model, name: str, measures: dict[str, str], value: object) -> list[str]:
    """Each variable's value, the result's type, and the measures that wrap this one.

    DEV-03 asked what [_ShowValueForDates] does: the model called MAXX({MAX(...)},
    ''[Value]) "flawed", said the measure returned a date and never gave the last date
    with data. Its variables' values (31 Dec 2009), its TRUE/FALSE result and the 20
    measures wrapped in IF([_ShowValueForDates], ...) are facts, not interpretations.
    Variables are computed without filters, so they are shown only when the measure
    itself gives the saved state's value that way.
    """
    from .dax_engine import Engine
    from .dax_syntax import TableCtor

    try:
        node = parse_expression(measures.get(name, ""))
    except ValueError:
        return []
    facts: list[str] = []
    engine = Engine(model, time_budget=20)
    unfiltered: object
    try:
        unfiltered = engine.evaluate_scalar("[" + name.replace("]", "]]") + "]")
    except (DaxEngineError, ValueError, LookupError, TypeError):
        unfiltered = object()
    body = node
    if isinstance(node, VarBlock):
        variables = dict(node.variables)
        body = node.body
        for _ in range(len(variables)):
            if isinstance(body, Name) and body.name in variables:
                body = variables[body.name]
    consistent = _fact_value(unfiltered) == _fact_value(value)
    computed: dict[str, object] = {}
    if isinstance(node, VarBlock) and consistent:
        for index, (variable, expression) in enumerate(node.variables):
            if isinstance(expression, TableCtor) or (
                isinstance(expression, Call)
                and expression.name in {"FILTER", "VALUES", "ALL", "TOPN"}
            ):
                continue
            try:
                result = engine.evaluate_scalar(
                    to_dax(VarBlock(node.variables[: index + 1], Name(variable)))
                )
            except (DaxEngineError, ValueError, LookupError, TypeError):
                continue
            text = f"{result:%d %b %Y}" if isinstance(result, datetime) else _fact_value(result)
            facts.append(f"- {variable} = {text}")
            computed[variable] = result
    if isinstance(body, Binary) and body.op in _COMPARISONS:
        facts.append(
            "- It returns TRUE or FALSE (the result is a comparison), not a number or date."
        )
        facts += _date_cutoff(engine, node, body, computed)
    if consistent:
        facts += _condition_effects(engine, node, unfiltered)
    if re.search(r"\{\s*[^{}]+\}", measures.get(name, "")) and "[Value]" in measures.get(name, ""):
        facts.append(
            "- { expression } is a one-row, one-column table whose column is named Value; "
            "MAXX over it (''[Value]) returns that single value."
        )
    ref = re.escape("[" + name + "]")
    wrapped = [
        other
        for other, text in measures.items()
        if other != name and re.search(rf"\bIF\s*\(\s*{ref}\s*,", text, re.I)
    ]
    if wrapped:
        listed = ", ".join(f"[{w}]" for w in wrapped[:4]) + (", …" if len(wrapped) > 4 else "")
        facts.append(
            f"- {len(wrapped)} measures return their value only when [{name}] is TRUE, as "
            f"IF([{name}], …), and blank otherwise: {listed}."
        )
    return facts
