"""The planner cases (planner_cases.jsonl) and how they are scored.

Each case is one chat message on one context (contexts.py) with what should come of it:

- ``{"kind": "plan", "steps": [...]}``: the edits asked for, in order. A step names its tool
  either by ``op`` (the operations of today, with ``slots`` by the card's slot names) or, for a
  family that may not be registered yet, by ``family`` / ``capability`` / ``op_like`` (a
  regular expression over op names) with ``slots_by_kind`` (``{"visual": "e82e02f9", "field":
  "Rank"}``, given to the card's first slot of that kind). A case whose tools are not all
  registered is skipped and counted, never failed.
- ``{"kind": "no_plan"}``: an edit request no tool does; nothing may be planned for it.
- ``{"kind": "not_edit"}``: a question or remark; nothing may be edited.
- ``{"kind": "unsupported", "capability": "..."}``: refused with that matrix entry.

Gold values: a visual by the first characters of its id, a page by name or id, a field by its
name, a color as a word or hex, a text as the words. ``pieces`` (optional) are the parts of a
multi-step message, one per step.

A ``dax`` slot (a measure's ``expression``, a role's ``filter``...) is scored by what the formula
computes, never by being non-empty. Its gold is either a reference formula (the stub model
drafts it; the spec below is read from it: its column/measure references and its outermost
operation) or a formula spec::

    {"formula": {
        "uses": ["FactSales[DiscountAmount]", "[Total Sales]"],  # every one must be referenced
        "op": "divide",                  # or a list; each must occur (see FORMULA_OPS)
        "numerator": ["FactSales[DiscountAmount]"],   # optional, for "divide": some ratio has
        "denominator": ["[Total Sales]"],             #   these above and these below the line
        "values": ["Online"],            # optional: string literals it must compare with
        "not_uses": ["[Profit]"],        # optional: references it must not make
     },
     "example": "DIVIDE(SUM(FactSales[DiscountAmount]), [Total Sales])"}  # what the stub drafts

A reference is satisfied directly or through a measure it names (``[Total Sales]`` reaches
``FactSales[SalesAmount]``; a gold ``[Total Sales]`` is also met by what that measure uses).
Every drafted formula must parse with Studio's DAX parser (``dax_syntax``), must not name the
measure it defines (a circular dependency) and must not divide a value by itself (always 1):
those are ``wrong_formulas`` even when the gold gives no spec. A drafted formula the gold gives
no spec for, a Power Query (``m``) formula and a ``no_apply`` case are ``unverified``: counted
apart, never as right plans.

A role whose request says what it may see ("only sees the Online channel") must get a filter:
without one the role sees every row, so the missing filter is a wrong slot.

``model_asks`` cases are right only when the planner asks (a ``clarify`` outcome: a question or
a choice); giving up is not asking.

Unasked values are judged on the operation that will be applied, after the family's build: a
color the build adds (a second, green rule nobody asked for) is unasked unless the request or
the gold names that hue, and a page or table is unasked unless it is where the request already
is (the page on screen; the table of a field the step names; the measures' home table for a
new measure).

``evaluate_stub`` runs the real planner with a model that answers each call with the gold
choice; ``evaluate_coverage`` routes each message the way the chat does, with no model at all;
``score_live`` (planner_live.py) scores what the real model answered.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any

import contexts

from ai_powerbi_analyst.studio import dax_syntax
from ai_powerbi_analyst.studio import edit_planner as planner
from ai_powerbi_analyst.studio.edit_capabilities import card_capability
from ai_powerbi_analyst.studio.edit_context import EditContext, hue_family
from ai_powerbi_analyst.studio.edit_text import find_colors
from ai_powerbi_analyst.studio.edit_tool_cards import SHAPES, VISUAL_TYPES
from ai_powerbi_analyst.studio.edit_tools import Slot, ToolCard
from ai_powerbi_analyst.studio.edit_types import ParseKind
from ai_powerbi_analyst.studio.imported_chat_edits import route_message

HERE = Path(__file__).resolve().parent
PLANNER_CASES = HERE / "planner_cases.jsonl"
RECALL_GATE = 0.95
TOP_K = 6
KINDS = {"plan", "no_plan", "not_edit", "unsupported"}


def load_planner_cases(path: Path | None = None) -> list[dict[str, Any]]:
    file = path or PLANNER_CASES
    cases = [json.loads(line) for line in file.read_text(encoding="utf-8").splitlines() if line]
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "two planner cases share an id"
    for case in cases:
        assert case["expect"]["kind"] in KINDS, case["id"]
    return cases


def context_of(case: dict[str, Any]) -> EditContext:
    return contexts.context_for(case["context"], dict(case.get("options", {})))


def gold_card(step: dict[str, Any], cards: tuple[ToolCard, ...]) -> ToolCard | None:
    """The card a gold step names, among the registered ones (None when it is not there)."""
    if "op" in step:
        return next((c for c in cards if c.op == step["op"]), None)
    family, capability, like = step.get("family"), step.get("capability"), step.get("op_like")
    for card in cards:
        if family and card.family != family:
            continue
        if like and not re.search(like, card.op):
            continue
        if capability and card_capability(card) != capability and not like:
            continue
        return card
    return None


def gold_slots(step: dict[str, Any], card: ToolCard) -> dict[str, Any]:
    """The gold values by the card's slot names. ``slots_by_kind`` names a slot by its kind
    ("visual", "field" for any field kind, "dax"...), optionally narrowed by a word of its name
    ("bool:banded" is the bool slot whose name has "banded"); an ``enum`` value goes to the
    choice slot that offers it."""
    if "slots" in step:
        return dict(step["slots"])
    found: dict[str, Any] = {}
    fields = {"field", "measure", "column", "text"}
    for key, value in step.get("slots_by_kind", {}).items():
        kind, _, hint = key.partition(":")
        wanted = fields if kind == "field" else {kind}
        for slot in card.slots:
            if slot.name in found or slot.kind not in wanted or hint not in slot.name:
                continue
            if kind == "enum" and value not in slot.choices:
                continue
            if kind == "field" and slot.kind == "text" and not re.search("field|column", slot.name):
                continue  # a text slot takes a field's name only when it is named for one
            found[slot.name] = value
            break
    return found


def pieces_of(case: dict[str, Any]) -> list[str]:
    return list(case.get("pieces") or [case["message"]])


def plan_steps(case: dict[str, Any]) -> list[dict[str, Any]]:
    return list(case["expect"].get("steps", []))


# --- matching a planned value with a gold one ---------------------------------------------------


def _name_of(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value)


_QUALIFIED = re.compile(r"^(?P<table>[^\[\]]+)\[(?P<name>[^\[\]]+)\]$")


def _gold_field(gold: Any) -> tuple[str | None, str]:
    """A gold field: "Rank", or "FactSales[StoreKey]" when the name alone is not unique."""
    found = _QUALIFIED.match(str(gold))
    return (found["table"], found["name"]) if found else (None, str(gold))


def _field_matches(gold: Any, actual: Any) -> bool:
    table, name = _gold_field(gold)
    if _name_of(actual).casefold() != name.casefold():
        return False
    return table is None or not isinstance(actual, dict) or actual.get("table") == table


def slot_matches(slot: Slot | None, gold: Any, actual: Any, context: EditContext) -> bool:
    kind = slot.kind if slot is not None else ""
    if actual is None:
        return False
    if kind == "visual":
        return isinstance(actual, str) and actual.startswith(str(gold))
    if kind == "visuals":
        return (
            isinstance(actual, list)
            and isinstance(gold, list)
            and sorted(a[:8] for a in actual) == sorted(str(g)[:8] for g in gold)
        )
    if kind == "page":
        page = context.page(str(actual))
        return actual == gold or (page is not None and page.name.casefold() == str(gold).casefold())
    if kind in {"field", "measure", "column"}:
        return _field_matches(gold, actual)
    if kind == "fields":
        items = actual if isinstance(actual, list) else []
        golds = gold if isinstance(gold, list) else [gold]
        return len(items) == len(golds) and all(map(_field_matches, golds, items))
    if kind == "color":
        wanted = find_colors(str(gold))
        want = hue_family(wanted[0].hex) if wanted else None
        return isinstance(actual, str) and (
            actual.upper() == str(gold).upper() or hue_family(actual) == want
        )
    if kind in {"number", "integer"} and isinstance(actual, int | float):
        return isinstance(gold, int | float) and math.isclose(actual, gold, abs_tol=0.051)
    if kind == "text":
        if isinstance(actual, list):  # a theme's colors
            return len(actual) == len(str(gold).split(","))
        return str(actual).strip(" '\"").casefold() == str(gold).strip(" '\"").casefold()
    if kind == "dax":
        return not formula_problems(gold, actual, context)
    if kind == "m":  # no Power Query parser here: present, and reported as unverified
        return bool(str(actual).strip())
    if kind == "enum" and slot is not None and slot.name in {"visual_type", "shape"}:
        names = VISUAL_TYPES if slot.name == "visual_type" else SHAPES
        return names.get(str(gold), gold) == names.get(str(actual), actual)
    return bool(actual == gold)


# --- what a drafted formula computes ----------------------------------------------------------

# A request's operation word -> what shows it in the formula (a function or an operator).
FORMULA_OPS: dict[str, frozenset[str]] = {
    "divide": frozenset({"DIVIDE", "/"}),
    "subtract": frozenset({"-"}),
    "add": frozenset({"+"}),
    "multiply": frozenset({"*"}),
    "concatenate": frozenset({"&", "CONCATENATE"}),
    "sum": frozenset({"SUM", "SUMX"}),
    "average": frozenset({"AVERAGE", "AVERAGEX", "AVERAGEA"}),
    "count": frozenset({"COUNT", "COUNTA", "COUNTX", "COUNTAX", "COUNTROWS"}),
    "distinctcount": frozenset({"DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK"}),
    "min": frozenset({"MIN", "MINX"}),
    "max": frozenset({"MAX", "MAXX"}),
    "equals": frozenset({"=", "==", "IN"}),
    "compare": frozenset({"=", "==", "<>", "<", ">", "<=", ">=", "IN"}),
    "calculate": frozenset({"CALCULATE"}),
    "filter": frozenset({"FILTER", "CALCULATE", "KEEPFILTERS"}),
}


def _nodes(node: Any) -> list[Any]:
    """Every node of a DAX syntax tree, the outermost first."""
    found = [node]
    if isinstance(node, dax_syntax.Call):
        for arg in node.args:
            found += _nodes(arg)
    elif isinstance(node, dax_syntax.Binary):
        found += _nodes(node.left) + _nodes(node.right)
    elif isinstance(node, dax_syntax.Unary):
        found += _nodes(node.operand)
    elif isinstance(node, dax_syntax.TableCtor):
        for row in node.rows:
            for item in row:
                found += _nodes(item)
    elif isinstance(node, dax_syntax.VarBlock):
        for _name, value in node.variables:
            found += _nodes(value)
        found += _nodes(node.body)
    return found


def _op_of(node: Any) -> str | None:
    if isinstance(node, dax_syntax.Call):
        return node.name.upper()
    if isinstance(node, dax_syntax.Binary):
        return node.op.upper()
    return None


def _measure_names(context: EditContext) -> dict[str, str]:
    return {m.name.casefold(): m.expression for m in context.measures}


def _refs(node: Any, context: EditContext, own: str | None, seen: frozenset[str] = frozenset()
          ) -> set[tuple[str, str | None, str]]:
    """The references under ``node``, through the measures it names: ("measure", None, name)
    and ("column", table or None, name), all case-folded."""
    measures = _measure_names(context)
    found: set[tuple[str, str | None, str]] = set()
    for item in _nodes(node):
        if not isinstance(item, dax_syntax.ColRef):
            continue
        name = item.column.casefold()
        if item.table is None and name in measures:
            found.add(("measure", None, name))
            if name in seen or name == (own or "").casefold():
                continue
            try:
                body = dax_syntax.parse_expression(measures[name])
            except dax_syntax.DaxParseError:
                continue
            found |= _refs(body, context, own, seen | {name})
        else:
            found.add(("column", (item.table or "").casefold() or None, name))
    return found


def _use_met(use: str, refs: set[tuple[str, str | None, str]], context: EditContext,
             own: str | None) -> bool:
    """``Table[Column]``, ``[Measure]`` or a bare name, among the references."""
    qualified = _QUALIFIED.match(use.strip())
    if qualified:
        table, name = qualified["table"].strip("'").casefold(), qualified["name"].casefold()
        return any(kind == "column" and n == name and t in {table, None} for kind, t, n in refs)
    name = use.strip().strip("[]").casefold()
    if any(n == name for _kind, _t, n in refs):
        return True
    body = _measure_names(context).get(name)
    if body is None or name == (own or "").casefold():
        return False
    try:  # a gold measure is also met by what it computes from
        wanted = {r for r in _refs(dax_syntax.parse_expression(body), context, own)
                  if r[0] == "column"}
    except dax_syntax.DaxParseError:
        return False
    return bool(wanted) and wanted <= refs


def formula_spec(gold: Any) -> dict[str, Any] | None:
    """The spec a gold ``dax`` value asks for (see the module docstring); None when the gold is
    no formula at all (a gold that only asks for a formula to be there)."""
    if isinstance(gold, dict):
        spec = gold.get("formula", gold)
        return dict(spec) if isinstance(spec, dict) else None
    if not isinstance(gold, str) or not gold.strip():
        return None
    try:
        node = dax_syntax.parse_expression(gold)
    except dax_syntax.DaxParseError:
        return None
    uses = []
    for item in _nodes(node):
        if isinstance(item, dax_syntax.ColRef):
            uses.append(f"{item.table}[{item.column}]" if item.table else f"[{item.column}]")
    top = _op_of(node)
    ops = [word for word, marks in FORMULA_OPS.items() if top in marks and word != "compare"]
    return {"uses": list(dict.fromkeys(uses)), "op": ops[:1]}


def formula_example(gold: Any) -> str:
    """The formula the stub model drafts for a gold ``dax`` value."""
    if isinstance(gold, dict):
        return str(gold.get("example") or "")
    return str(gold)


def formula_problems(
    gold: Any, actual: Any, context: EditContext, own: str | None = None
) -> list[str]:
    """Why a drafted formula does not compute what the gold asks for ([] when it does). The
    problems name references and operations only, never the formula itself."""
    text = actual if isinstance(actual, str) else ""
    if not text.strip():
        return ["no formula"]
    try:
        node = dax_syntax.parse_expression(text.lstrip().removeprefix("=").strip())
    except dax_syntax.DaxParseError:
        return ["the formula does not parse as DAX"]
    problems = formula_defects(node, context, own)
    spec = formula_spec(gold)
    if spec is None:
        return problems
    refs = _refs(node, context, own)
    for use in spec.get("uses", []):
        if not _use_met(use, refs, context, own):
            problems.append(f"does not use {use}")
    for use in spec.get("not_uses", []):
        if _use_met(use, refs, context, own):
            problems.append(f"uses {use}, which the request rules out")
    present = {op for item in _nodes(node) if (op := _op_of(item))}
    wanted_ops = spec.get("op") or []
    for word in [wanted_ops] if isinstance(wanted_ops, str) else wanted_ops:
        if not FORMULA_OPS.get(word, frozenset({word.upper()})) & present:
            problems.append(f"does not {word}")
    if spec.get("numerator") or spec.get("denominator"):
        ratios = [
            (item.args[0], item.args[1]) if isinstance(item, dax_syntax.Call) else
            (item.left, item.right)
            for item in _nodes(node)
            if (isinstance(item, dax_syntax.Call) and item.name.upper() == "DIVIDE"
                and len(item.args) >= 2)
            or (isinstance(item, dax_syntax.Binary) and item.op == "/")
        ]
        if not any(
            all(_use_met(u, _refs(top, context, own), context, own)
                for u in spec.get("numerator", []))
            and all(_use_met(u, _refs(bottom, context, own), context, own)
                    for u in spec.get("denominator", []))
            for top, bottom in ratios
        ):
            problems.append("does not divide the requested numerator by the requested denominator")
    strings = {item.value.casefold() for item in _nodes(node) if isinstance(item, dax_syntax.Str)}
    for value in spec.get("values", []):
        if str(value).casefold() not in strings:
            problems.append(f"does not compare with {value!r}")
    return problems


def formula_defects(node: Any, context: EditContext, own: str | None) -> list[str]:
    """What is wrong with any formula, whatever it was asked to compute."""
    problems: list[str] = []
    if own and ("measure", None, own.casefold()) in {
        ("measure", None, i.column.casefold())
        for i in _nodes(node) if isinstance(i, dax_syntax.ColRef) and i.table is None
    }:
        problems.append(f"names the measure it defines ([{own}]): a circular dependency")
    for item in _nodes(node):
        pair = (
            (item.args[0], item.args[1]) if isinstance(item, dax_syntax.Call)
            and item.name.upper() == "DIVIDE" and len(item.args) >= 2
            else (item.left, item.right) if isinstance(item, dax_syntax.Binary) and item.op == "/"
            else None
        )
        if pair is not None and pair[0] == pair[1]:
            problems.append("divides a value by itself: always 1")
    return problems


# A role request that says what the role may see: without a filter it would see every row.
_ROLE_LIMIT = re.compile(
    r"\b(?:only\s+(?:sees?|see|shows?|gets?)|sees?\s+only|can\s+see\s+only|"
    r"(?:limited|restricted)\s+to|only\s+(?:for|to)\s+the)\b",
    re.I,
)
ROLE_OPS = frozenset({"create_role", "update_role"})


# --- the stub model -------------------------------------------------------------------------------


def _prompt_value(slot: Slot, value: Any, lists: Any) -> Any:
    """A gold value as the model writes it: a number from the prompt's lists (every list is
    numbered from 1), or the value."""
    kind = slot.kind
    if kind == "visual":
        return next(
            (n for n, e in enumerate(lists.visuals, 1) if e.id.startswith(str(value))), None
        )
    if kind == "visuals":
        numbers = [_prompt_value(Slot("v", "visual", ""), v, lists) for v in value]
        return None if None in numbers else numbers
    if kind == "page":
        wanted = str(value).casefold()
        return next(
            (
                n
                for n, p in enumerate(lists.pages, 1)
                if p.id.casefold() == wanted or p.name.casefold() == wanted
            ),
            None,
        )
    if kind in {"field", "measure", "column", "fields"}:
        names = value if isinstance(value, list) else [value]
        numbers = []
        for gold in names:
            table, name = _gold_field(gold)
            found = next(
                (
                    n
                    for n, f in enumerate(lists.fields, 1)
                    if f[2].casefold() == name.casefold()
                    and (table is None or f[1] == table)
                    and (kind not in {"measure", "column"} or f[0] == kind)
                ),
                None,
            )
            if found is None:
                return None
            numbers.append(found)
        return numbers if kind == "fields" else numbers[0]
    if kind == "table":
        return next((n for n, t in enumerate(lists.tables, 1) if t == value), None)
    return value


class GoldModel:
    """A stand-in for the local model that answers with the case's gold choice: the planner's
    retrieval, prompt, schema, slot reading, formula check and operation building all run for
    real. ``misses`` are gold values the prompt did not offer (a retrieval or context miss)."""

    def __init__(self, case: dict[str, Any], cards: tuple[ToolCard, ...]) -> None:
        self.case = case
        self.cards = cards
        self.planner: planner.EditPlanner | None = None
        self.misses: list[str] = []
        self.calls: list[str] = []
        self.formulas = [
            formula_example(value)
            for step in plan_steps(case)
            if (card := gold_card(step, cards)) is not None
            for name, value in gold_slots(step, card).items()
            if (slot := card.slot(name)) is not None and slot.kind in {"dax", "m"}
        ]

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[Any],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> Any:
        if issubclass(response_model, planner.DraftAnswer):
            self.calls.append("draft")
            formula = self.formulas.pop(0) if self.formulas else "1"
            return response_model(expression=formula)
        self.calls.append("choose")
        assert self.planner is not None and self.planner.prompt is not None
        prompt = self.planner.prompt
        steps: list[dict[str, Any]] = []
        for step in plan_steps(self.case):
            card = gold_card(step, self.cards)
            if card is None or card not in prompt.cards:
                self.misses.append(f"tool {step.get('op') or step.get('op_like')} not offered")
                continue
            slots: dict[str, Any] = {}
            for name, value in gold_slots(step, card).items():
                slot = card.slot(name)
                if slot is None or slot.kind in {"dax", "m"}:
                    continue
                converted = _prompt_value(slot, value, prompt.lists)
                if converted is None:
                    self.misses.append(f"{card.op}.{name}={value!r} not in the prompt's lists")
                    continue
                slots[name] = converted
            steps.append({"tool": prompt.cards.index(card) + 1, "slots": slots})
        steps = self.noise(prompt, steps)
        return response_model.model_validate(
            {"kind": "plan" if steps else "unclear", "steps": steps[: planner.MAX_STEPS]}
        )

    def noise(self, prompt: planner.Prompt, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The gold answer as it is (``NoisyModel`` changes it)."""
        return steps


INVENTED_TEXT = "Invented by the model"
INVENTED_COLOR = "#123456"
INVENTED_NUMBER = 37


class NoisyModel(GoldModel):
    """The gold answer with one thing a 4B model makes up, by turns: a value for a slot the
    request does not give, a step nobody asked for, another visual than the one named, another
    page than the one named. The planner must keep none of them (``evaluate_stub(noisy=True)``
    counts what it kept)."""

    def __init__(self, case: dict[str, Any], cards: tuple[ToolCard, ...], turn: int) -> None:
        super().__init__(case, cards)
        self.turn = turn
        self.invented: list[tuple[int, str, Any]] = []  # (step, slot, the value as converted)
        self.kind = ""

    def noise(self, prompt: planner.Prompt, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not steps:
            return steps
        for attempt in range(4):
            kind = ("value", "step", "visual", "page")[(self.turn + attempt) % 4]
            changed = self._apply(kind, prompt, [dict(s, slots=dict(s["slots"])) for s in steps])
            if changed is not None:
                self.kind = kind
                return changed
        return steps

    def _apply(
        self, kind: str, prompt: planner.Prompt, steps: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        lists = prompt.lists
        first = steps[0]
        card = prompt.cards[first["tool"] - 1]
        if kind == "value":
            for slot in card.slots:
                if slot.required or slot.name in first["slots"]:
                    continue
                made = _made_up(slot)
                if made is None:
                    continue
                first["slots"][slot.name] = made[0]
                self.invented.append((0, slot.name, made[1]))
                return steps
            return None
        if kind == "step":
            if not lists.visuals:
                return None
            visual = next((s for s in card.slots if s.kind == "visual"), None)
            extra = dict(first, slots=dict(first["slots"]))
            if visual is not None:
                other = next(
                    (
                        n
                        for n, _e in enumerate(lists.visuals, 1)
                        if n != first["slots"].get(visual.name)
                    ),
                    None,
                )
                if other is None:
                    return None
                extra["slots"][visual.name] = other
            self.invented.append((len(steps), "", None))
            return [*steps, extra]
        if kind == "visual":
            visual = next((s for s in card.slots if s.kind == "visual"), None)
            if visual is None or visual.name not in first["slots"]:
                return None
            now = first["slots"][visual.name]
            other = next(
                (
                    n
                    for n, e in enumerate(lists.visuals, 1)
                    if n != now
                    and (not card.visual_types or e.visual_type in card.visual_types)
                ),
                None,
            )
            if other is None:
                return None
            first["slots"][visual.name] = other
            self.invented.append((0, visual.name, lists.visuals[other - 1].id))
            return steps
        page = next((s for s in card.slots if s.kind == "page"), None)
        if page is None or len(lists.pages) < 2:
            return None
        now = first["slots"].get(page.name)
        other = next((n for n in range(1, len(lists.pages) + 1) if n != now), None)
        if other is None:
            return None
        first["slots"][page.name] = other
        self.invented.append((0, page.name, lists.pages[other - 1].id))
        return steps


def _made_up(slot: Slot) -> tuple[Any, Any] | None:
    """A value for a slot the request does not give: (as the model writes it, as the planner
    would keep it)."""
    if slot.kind == "bool":
        return True, True
    if slot.kind in {"number", "integer"}:
        low = slot.minimum if slot.minimum is not None else 0
        high = slot.maximum if slot.maximum is not None else 1000
        value = INVENTED_NUMBER if low <= INVENTED_NUMBER <= high else low + (high - low) / 3
        value = int(value) if slot.kind == "integer" else value
        return value, value
    if slot.kind == "color":
        return INVENTED_COLOR, INVENTED_COLOR
    if slot.kind == "enum" and slot.choices:
        return slot.choices[-1], slot.choices[-1]
    if slot.kind == "text":
        return INVENTED_TEXT, INVENTED_TEXT
    return None


def kept_noise(model: NoisyModel, outcome: planner.PlanOutcome, gold_steps: int) -> list[str]:
    """What the plan kept of what the noisy model made up (a made-up value that happens to be
    one the request gives, or the gold's own, does not count)."""
    kept: list[str] = []
    golds = [
        str(v)[:8] if isinstance(v, str) else v
        for step in plan_steps(model.case)
        for v in (step.get("slots") or step.get("slots_by_kind") or {}).values()
    ]
    for index, name, value in model.invented:
        if not name:
            if len(outcome.steps) > gold_steps:
                kept.append(f"kept a step nobody asked for ({len(outcome.steps)} > {gold_steps})")
            continue
        short = str(value)[:8] if isinstance(value, str) else value
        if short in golds or (
            isinstance(value, str)
            and re.search(planner._choice_pattern(value), model.case["message"], re.I)
        ):
            continue
        if index < len(outcome.steps):
            step = outcome.steps[index]
            got = step.slots.get(name)
            same = got == value or (
                isinstance(got, str) and isinstance(value, str) and got.upper() == value.upper()
            )
            # a choice that names another slot the step fills ("target: field" with the field)
            # is what that slot implies, not made up
            implied = isinstance(value, str) and step.slots.get(value) is not None
            if same and name == "page" and step.op == "add_button":
                gold = plan_steps(model.case)
                card = gold_card(gold[index], model.cards) if index < len(gold) else None
                wanted = gold_slots(gold[index], card) if card is not None else {}
                context = context_of(model.case)
                active = context.active_page or (context.pages[0].id if context.pages else None)
                implied = implied or (
                    "page" not in wanted and "target_page" in wanted
                    and value == active and step.operation.get("page") == active
                )
            if same and not implied:
                kept.append(f"kept the made-up {name} {value!r}")
    return kept


# --- scoring one planned case -----------------------------------------------------------------


def effective_slots(step: planner.PlanStep, card: ToolCard) -> dict[str, Any]:
    """Score what will be applied, including values a builder derives from its slots."""
    found = dict(step.slots)
    for slot in card.slots:
        value: Any = step.operation
        for part in (slot.field or slot.name).split("."):
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            if card.op == "create_hierarchy" and slot.name == "levels" and isinstance(value, list):
                value = [
                    {"name": item["column"], "table": step.operation.get("table")}
                    if isinstance(item, dict) and "column" in item else item
                    for item in value
                ]
            found[slot.name] = value
    # Relationship builders split a qualified column into the operation's two fields.
    for side in ("from", "to"):
        if card.slot(side) is not None and f"{side}_column" in step.operation:
            found[side] = {
                "table": step.operation.get(f"{side}_table"),
                "name": step.operation[f"{side}_column"],
            }
    if card.op == "mark_date_table" and "column" in step.operation:
        found["date_column"] = {
            "table": step.operation.get("table"), "name": step.operation["column"]
        }
    # Role builders keep a single table filter as {"table", "expression"} under "filters".
    filters = step.operation.get("filters")
    if card.op in ROLE_OPS and isinstance(filters, list) and filters:
        first = filters[0] if isinstance(filters[0], dict) else {}
        if card.slot("table") is not None and first.get("table"):
            found["table"] = first["table"]
        if card.slot("filter") is not None and "expression" in first:
            found["filter"] = first["expression"]
    return found


_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _colors_in(value: Any) -> list[str]:
    """Every #RRGGBB the operation writes, wherever it is (rules, gradients, fonts...)."""
    if isinstance(value, dict):
        return [c for item in value.values() for c in _colors_in(item)]
    if isinstance(value, list):
        return [c for item in value for c in _colors_in(item)]
    return [value.upper()] if isinstance(value, str) and _HEX_COLOR.match(value) else []


def _hues(value: Any) -> set[str]:
    """The hues a gold value, a slot value or the request's words name."""
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found |= _hues(item)
    elif isinstance(value, list):
        for item in value:
            found |= _hues(item)
    elif isinstance(value, str):
        if _HEX_COLOR.match(value):
            hue = hue_family(value)
            found |= {hue} if hue else set()
        else:
            found |= {h for m in find_colors(value) if (h := hue_family(m.hex))}
    return found


def _built_colors_unasked(
    case: dict[str, Any], wanted: dict[str, Any], actual: planner.PlanStep
) -> list[str]:
    """Colors the applied operation writes that nobody asked for: not a hue the request or the
    gold names, nor one the model's own slots gave (those are judged slot by slot)."""
    asked = _hues(case.get("message", "")) | _hues(list(wanted.values()))
    asked |= _hues(list(actual.slots.values()))
    return [
        color for color in dict.fromkeys(_colors_in(actual.operation))
        if hue_family(color) not in asked
    ]


def _home_table(context: EditContext) -> str | None:
    """The table most measures live in (where a new measure goes)."""
    counts: dict[str, int] = {}
    for measure in context.measures:
        counts[measure.table] = counts.get(measure.table, 0) + 1
    return max(counts, key=lambda t: counts[t]) if counts else None


def _implied_place(
    card: ToolCard, name: str, value: Any, filled: dict[str, Any], context: EditContext
) -> bool:
    """A page or table nobody named is right only where the request already is: the page on
    screen; the table of a field (or of a formula's column) the step names; the measures' home
    table for a new measure."""
    slot = card.slot(name)
    if name == "page":  # the placement: the page on screen
        page = context.active_page or (context.pages[0].id if context.pages else None)
        return page is not None and slot_matches(slot, page, value, context)
    if slot is not None and slot.kind == "page":
        return False  # a destination nobody named is never implied
    table = _name_of(value).casefold()
    tables: set[str] = set()
    for other, item in filled.items():
        if other == name:
            continue
        for entry in item if isinstance(item, list) else [item]:
            if isinstance(entry, dict) and entry.get("table"):
                tables.add(str(entry["table"]).casefold())
        other_slot = card.slot(other)
        # a role's table is the one its filter reads (a measure's home is no formula's table)
        if (card.op in ROLE_OPS and other_slot is not None and other_slot.kind == "dax"
                and isinstance(item, str)):
            try:
                node = dax_syntax.parse_expression(item)
            except dax_syntax.DaxParseError:
                continue
            columns = {i.column.casefold() for i in _nodes(node)
                       if isinstance(i, dax_syntax.ColRef)}
            tables |= {i.table.casefold() for i in _nodes(node)
                       if isinstance(i, dax_syntax.ColRef) and i.table}
            tables |= {t.casefold() for t, cols in context.tables.items()
                       if columns & {c.casefold() for c in cols}}
    if table in tables:
        return True
    home = _home_table(context)
    return "measure" in card.op and home is not None and table == home.casefold()


def _implied_setting(
    card: ToolCard,
    name: str,
    value: Any,
    wanted: dict[str, Any],
    actual: dict[str, Any],
    operation: dict[str, Any],
    context: EditContext,
) -> bool:
    """Only defaults/equivalences proven by the requested operation and its context."""
    if card.op == "add_button" and name == "action":
        # AddButton(kind="back") already writes Back when no action is supplied.
        # Its explicit equivalent does not excuse an unrelated destination slot.
        return wanted.get("kind") == actual.get("kind") == value == "back"
    if card.op == "set_small_multiples" and name == "small_multiples":
        return (
            value is True
            and operation.get("remove") is not True
            and "field" in wanted
            and slot_matches(card.slot("field"), wanted["field"], actual.get("field"), context)
        )
    if card.op == "mark_date_table" and name == "date_column" and "table" in wanted:
        table = wanted["table"]
        dates = [
            column for column in context.tables.get(table, [])
            if context.column_types.get(f"{table}.{column}", "").casefold().startswith("date")
        ]
        return len(dates) == 1 and value == {"table": table, "name": dates[0]}
    return False


def score_outcome(
    case: dict[str, Any],
    outcome: planner.PlanOutcome,
    cards: tuple[ToolCard, ...],
    context: EditContext,
    checker: Any,
) -> dict[str, Any]:
    """Tool and slot accuracy of a plan against the case's gold, whether its operations apply to
    the twin, what its drafted formulas compute, and what could not be verified."""
    gold = plan_steps(case)
    made = list(outcome.steps)
    message = str(case.get("message", ""))
    problems: list[str] = []
    unverified: list[str] = []
    wrong_formulas = 0
    tools_ok = slots_total = slots_ok = 0
    for index, step in enumerate(gold):
        card = gold_card(step, cards)
        wanted = (
            gold_slots(step, card) if card is not None
            else dict(step.get("slots") or step.get("slots_by_kind") or {})
        )
        slots_total += len(wanted)
        # a role the request limits ("only sees the Online channel") needs its filter
        role_limit = (
            card is not None and card.op in ROLE_OPS and "filter" not in wanted
            and _ROLE_LIMIT.search(message) is not None
        )
        slots_total += 1 if role_limit else 0
        actual = made[index] if index < len(made) else None
        if card is None or actual is None or actual.op != card.op:
            problems.append(
                f"step {index + 1}: {actual.op if actual else 'nothing'} where "
                f"{card.op if card else step} was due"
            )
            continue
        tools_ok += 1
        filled = effective_slots(actual, card)
        named = actual.operation.get("name")
        own = named if isinstance(named, str) else None  # the measure a formula defines
        for name, value in wanted.items():
            slot = card.slot(name)
            if slot is not None and slot.kind == "dax":
                found = formula_problems(value, filled.get(name), context,
                                         own if name == "expression" else None)
                if found:
                    problems += [f"step {index + 1} {name}: {p}" for p in found]
                else:
                    slots_ok += 1
                if formula_spec(value) is None:
                    unverified.append(f"step {index + 1} {name}: the gold gives no formula spec")
                continue
            if slot_matches(slot, value, filled.get(name), context):
                slots_ok += 1
            else:
                problems.append(f"step {index + 1} {name}: {filled.get(name)!r} != {value!r}")
        for slot in card.slots:
            value = filled.get(slot.name)
            if slot.name in wanted or value is None or slot.kind not in {"dax", "m"}:
                continue
            if slot.kind == "m":
                unverified.append(f"step {index + 1} {slot.name}: Power Query is not checked")
                continue
            # a formula the gold does not specify: its plain defects still make it wrong
            found = formula_problems(None, value, context,
                                     own if slot.name == "expression" else None)
            if found:
                wrong_formulas += 1
                problems += [f"step {index + 1} {slot.name}: {p}" for p in found]
            if not (role_limit and slot.name == "filter"):
                unverified.append(
                    f"step {index + 1} {slot.name}: drafted formula the gold gives no spec for"
                )
        if role_limit:
            if isinstance(filled.get("filter"), str) and filled["filter"].strip():
                slots_ok += 1
                unverified.append(
                    f"step {index + 1} filter: the gold does not say what the filter keeps"
                )
            else:
                problems.append(
                    f"step {index + 1} filter: the request limits what the role sees, "
                    "the plan sets no filter (the role would see every row)"
                )
    if len(made) > len(gold):
        problems.append(f"{len(made) - len(gold)} step(s) nobody asked for")
    unasked = 0
    for index, step in enumerate(gold):
        card = gold_card(step, cards)
        if card is None or index >= len(made) or made[index].op != card.op:
            continue
        wanted = gold_slots(step, card)
        actual = made[index]
        filled = effective_slots(actual, card)
        # Builders supply defaults and derived aliases without the model inventing a slot.
        # Compare against the actual builder output, not a blanket list of allowed names.
        try:
            built = card.to_operation(actual.slots)
        except (KeyError, TypeError, ValueError):
            defaults: dict[str, Any] = {}
        else:
            defaults = effective_slots(planner.PlanStep(card.op, built, ""), card)
        for name, value in filled.items():
            slot = card.slot(name)
            if name in wanted or slot is None or slot.kind in {"dax", "m"}:
                continue
            if name == "page" and card.op == "add_button" and "target_page" in wanted:
                page = context.active_page or (context.pages[0].id if context.pages else None)
                if page is not None and not slot_matches(slot, page, value, context):
                    unasked += 1
                    problems.append(
                        f"step {index + 1} page: {value!r} is not the active page {page!r}"
                    )
                continue
            if name in {"page", "table"} or slot.kind in {"page", "table"}:
                # where it goes: right only where the request already is (a builder's default
                # place is no excuse: it is still a page or table nobody named)
                if not _implied_place(card, name, value, filled, context):
                    unasked += 1
                    problems.append(f"step {index + 1} {name}: {_name_of(value)!r} not asked for")
                continue
            if name not in actual.slots and name in defaults and value == defaults[name]:
                continue
            if slot.kind == "visual" and not any(
                s.kind == "visual" and s.name in wanted for s in card.slots
            ):
                continue  # the selection
            if _implied_setting(card, name, value, wanted, filled, actual.operation, context):
                continue
            if name != "action" and isinstance(value, str) and filled.get(value) is not None:
                continue  # a choice another filled slot implies ("target: field")
            unasked += 1
            problems.append(f"step {index + 1} {name}: {value!r} not asked for")
        # what the family's build adds to the applied operation: a color nobody named
        for color in _built_colors_unasked(case, wanted, actual):
            unasked += 1
            problems.append(f"step {index + 1}: the operation adds {color} nobody asked for")
    # ("no_apply": a case whose operation the twin cannot take, for a reason the case gives;
    # its plan is reported as unverified, never as a checked right plan)
    if case.get("no_apply"):
        applied: list[str] = []
        if made:
            unverified.append(f"not applied to the twin: {case['no_apply']}")
    else:
        applied = checker.check_ops(tuple(step.operation for step in made), case["context"])
    problems += applied
    return {
        "tools": len(gold),
        "tools_ok": tools_ok,
        "slots": slots_total,
        "slots_ok": slots_ok,
        "extra_steps": max(0, len(made) - len(gold)),
        "unasked": unasked,
        "invalid": len(applied),
        "wrong_formulas": wrong_formulas,
        "unverified": unverified,
        "problems": problems,
    }


def plan_is_right(scored: dict[str, Any]) -> bool:
    """Every tool and slot right, nothing unasked, nothing invalid, no broken formula (whether
    it was also verified is ``scored["unverified"]``)."""
    return (
        scored["tools_ok"] == scored["tools"]
        and scored["slots_ok"] == scored["slots"]
        and scored["extra_steps"] == 0
        and scored["unasked"] == 0
        and scored["invalid"] == 0
        and scored.get("wrong_formulas", 0) == 0
    )


def asks_back(outcome: planner.PlanOutcome) -> bool:
    """A ``model_asks`` case is right only when the planner asks: a question or a choice."""
    return outcome.kind == "clarify"


def _percentile(values: list[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)], 3)


def available(case: dict[str, Any], cards: tuple[ToolCard, ...]) -> bool:
    return all(gold_card(step, cards) is not None for step in plan_steps(case))


def evaluate_stub(
    cases: list[dict[str, Any]], only: set[str] | None = None, *, noisy: bool = False
) -> dict[str, Any]:
    """Every plan case through the real planner with the gold-answering model; with
    ``noisy``, a model that also makes something up in every answer (the planner must keep
    none of it). A case marked ``model_asks`` is one the words do not settle (which four of
    the six cards): the planner must ask (a question or a choice), never plan or give up."""
    import run  # noqa: PLC0415 - the parser eval's operation checker (a sibling module)

    cards = planner.available_cards()
    checker = run.OperationChecker()
    selected = [c for c in cases if (not only or c["id"] in only)]
    planned = [c for c in selected if c["expect"]["kind"] == "plan"]
    # a case a family's card cannot express yet is listed with the reason, never counted
    blocked = {c["id"]: c["blocked_by"] for c in planned if c.get("blocked_by")}
    runnable = [c for c in planned if available(c, cards) and c["id"] not in blocked]
    skipped = [c["id"] for c in planned if not available(c, cards)]
    findings: list[dict[str, Any]] = []
    hits = steps = offered = 0
    tools = tools_ok = slots = slots_ok = invalid = misses = extra = unasked = 0
    wrong_formulas = 0
    unverified: dict[str, list[str]] = {}
    asked_ok = asked = 0
    noise_cases = noise_kept = noise_still_right = 0
    noise_kinds: dict[str, int] = {}
    sizes: list[float] = []
    timings: list[float] = []
    # The noise kind goes by turns over the plan cases in file order. A styling case (a plan
    # until Studio stopped changing styling, now a refusal tagged "styling") keeps its turn, so
    # every case after it gets the noise it had before.
    turns: dict[str, int] = {}
    for case in selected:
        if case["expect"]["kind"] == "plan" or "styling" in case.get("tags", []):
            turns[case["id"]] = len(turns)
    for case in runnable:
        turn = turns[case["id"]]
        context = context_of(case)
        pieces = pieces_of(case)
        gold = plan_steps(case)
        problems: list[str] = []
        for index, step in enumerate(gold):
            steps += 1
            piece = step.get("piece") or (pieces[index] if len(pieces) == len(gold) else pieces[0])
            found = [s.card.op for s in planner.retrieve(piece, context, cards, limit=TOP_K)]
            card = gold_card(step, cards)
            if card is not None and card.op in found:
                hits += 1
            else:
                problems.append(f"retrieval: {card.op if card else step} not in {found}")
        model = NoisyModel(case, cards, turn) if noisy else GoldModel(case, cards)
        started = time.perf_counter()
        made = planner.EditPlanner(context, model, cards=cards, check_m_expression=_no_node)
        model.planner = made
        outcome = made.plan(pieces)
        timings.append((time.perf_counter() - started) * 1000)
        if made.prompt is not None:
            sizes.append(made.prompt.size)
            offered += sum(1 for step in gold if gold_card(step, cards) in made.prompt.cards)
        misses += len(model.misses)
        problems += [f"stub: {m}" for m in model.misses]
        scored = score_outcome(case, outcome, cards, context, checker)
        if isinstance(model, NoisyModel) and model.invented:
            noise_cases += 1
            noise_kinds[model.kind] = noise_kinds.get(model.kind, 0) + 1
            kept = kept_noise(model, outcome, len(gold))
            noise_kept += len(kept)
            problems += kept
            if outcome.kind == "plan" and not kept and scored["tools_ok"] == len(gold):
                noise_still_right += 1
        if case.get("model_asks"):
            # the words do not settle it: a question or a choice, never a plan or a give-up
            asked += 1
            if asks_back(outcome):
                asked_ok += 1
                if problems or noisy:
                    findings.append(
                        {"id": case["id"], "message": case["message"], "wrong": [],
                         "problems": problems}
                    )  # fmt: skip
                continue
            problems.append(
                "planned a case the words do not settle" if outcome.kind == "plan"
                else f"gave up ({outcome.kind}) instead of asking"
            )
        if outcome.kind != "plan" and not noisy:
            problems.append(f"outcome {outcome.kind}: {outcome.reason or outcome.clause}")
        tools += scored["tools"]
        tools_ok += scored["tools_ok"]
        slots += scored["slots"]
        slots_ok += scored["slots_ok"]
        invalid += scored["invalid"]
        extra += scored["extra_steps"]
        unasked += scored["unasked"]
        wrong_formulas += scored["wrong_formulas"]
        if scored["unverified"]:
            unverified[case["id"]] = scored["unverified"]
        if not noisy:
            problems += scored["problems"]
        else:
            problems += [p for p in scored["problems"] if "nobody asked" in p or "not asked" in p]
            problems += [p for p in scored["problems"] if p.startswith("Step")]
        if problems:
            findings.append(
                {"id": case["id"], "message": case["message"], "wrong": [], "problems": problems}
            )
    recall = hits / steps if steps else 1.0
    validity = tools_ok / tools if tools else 1.0
    report: dict[str, Any] = {
        "mode": "planner-noisy" if noisy else "planner-stub",
        "cases": len(selected),
        "plan_cases": len(planned),
        "evaluated": len(runnable),
        "skipped_family_not_registered": len(skipped),
        "skipped_ids": skipped,
        "blocked_by_a_family_card": blocked,
        "gold_steps": steps,
        "retrieval_recall_at_6": round(recall, 4),
        "offered_in_prompt": round(offered / steps, 4) if steps else 1.0,
        "slot_to_op_validity": round(validity, 4),
        "slot_accuracy": round(slots_ok / slots, 4) if slots else 1.0,
        "invalid_operations": invalid,
        "extra_steps": extra,
        "unasked_slots": unasked,
        "wrong_formulas": wrong_formulas,
        "unverified": unverified,
        "asked_where_words_do_not_settle": f"{asked_ok}/{asked}",
        "context_misses": misses,
        "prompt_bytes": {
            "p50": statistics.median(sizes) if sizes else 0,
            "max": max(sizes) if sizes else 0,
            "budget": planner.PROMPT_BUDGET,
        },
        "latency_ms": {"p50": _percentile(timings, 0.5), "p95": _percentile(timings, 0.95)},
        "findings": findings,
    }
    report["gates"] = {
        "recall@6>=95%": recall >= RECALL_GATE,
        "prompt<=budget": (max(sizes) if sizes else 0) <= planner.PROMPT_BUDGET,
        "asks_where_words_do_not_settle": asked_ok == asked,
    }
    if noisy:
        report["noise"] = {
            "answers_with_something_made_up": noise_cases,
            "by_kind": noise_kinds,
            "made_up_values_or_steps_kept": noise_kept,
            "still_the_right_plan": noise_still_right,
        }
        report["gates"]["nothing_made_up_kept"] = noise_kept == 0 and extra == 0 and unasked == 0
        report["gates"]["no_invalid_operations"] = invalid == 0 and wrong_formulas == 0
    else:
        report["gates"]["slot_to_op_validity==100%"] = (
            validity == 1.0 and invalid == 0 and extra == 0 and unasked == 0
            and wrong_formulas == 0
        )
    report["passed"] = all(report["gates"].values())
    return report


def _no_node(expression: str, context: EditContext) -> list[str]:
    """No Power Query validator run in the stub eval (it needs Node): the formula is the gold."""
    return []


# --- coverage of the deterministic tier -------------------------------------------------------


def deterministic_outcome(case: dict[str, Any]) -> dict[str, Any]:
    """The message routed the way the chat routes it, with no model: whether the planner would
    be called, and what the deterministic tier made."""
    context = context_of(case)
    routed = route_message(case["message"], context)
    if routed is None:
        return {
            "planner": False, "edits": [], "ops": [], "kinds": ["question"], "capabilities": []
        }  # fmt: skip
    clauses = routed.result.clauses
    made = {ParseKind.APPLY, ParseKind.MODEL_EDIT}
    ops = [dict(op) for c in clauses if c.kind in made for op in c.ops]
    return {
        "planner": bool(routed.result.unparsed),
        "edits": [str(op.get("op")) for op in ops],
        "ops": ops,
        "kinds": [c.kind.value for c in clauses],
        "capabilities": [c.capability for c in clauses if c.kind is ParseKind.UNSUPPORTED],
    }


def _op_value(op: dict[str, Any], slot: Slot) -> Any:
    target: Any = op
    for part in (slot.field or slot.name).split("."):
        if not isinstance(target, dict) or part not in target:
            return None
        target = target[part]
    return target


def wrong_reads(
    case: dict[str, Any], got: dict[str, Any], cards: tuple[ToolCard, ...]
) -> list[str]:
    """What the no-model tier made that contradicts the gold: another operation, the opposite
    on/off, or another number than the one the request gives."""
    if not got["ops"]:
        return []
    found: list[str] = []
    numbers = planner._numbers_in(case["message"])
    gold = plan_steps(case)
    for step in gold:
        card = gold_card(step, cards)
        if card is None:
            continue
        mine = [op for op in got["ops"] if op.get("op") == card.op]
        if not mine:
            if len(got["ops"]) >= len(gold):
                # something else applied in its place (a question for it is no wrong edit)
                found.append(f"made {got['edits']} where {card.op} was due")
            continue
        for name, value in gold_slots(step, card).items():
            slot = card.slot(name)
            if slot is None or card.build is not None:
                continue  # a built operation's fields are not the slots' names
            if slot.kind == "bool" and isinstance(value, bool):
                values = [_op_value(op, slot) for op in mine]
                if value not in values:
                    found.append(f"{card.op}.{name}: {values} where {value} was due")
            elif slot.kind in {"number", "integer"} and isinstance(value, int | float):
                said = any(math.isclose(value, n, abs_tol=1e-6) for n in numbers)
                values = [v for op in mine if (v := _op_value(op, slot)) is not None]
                # (not set at all: it already has that value; only another number is wrong)
                if said and values and not any(
                    isinstance(v, int | float) and math.isclose(v, value, abs_tol=0.051)
                    for v in values
                ):
                    found.append(f"{card.op}.{name}: {values} where {value} was due")
    return found


def evaluate_coverage(cases: list[dict[str, Any]], only: set[str] | None = None) -> dict[str, Any]:
    """How much of the planner cases the deterministic tier reads alone, and how well."""
    cards = planner.available_cards()
    selected = [c for c in cases if (not only or c["id"] in only)]
    handled = correct = false_edits = wrong_total = 0
    by_kind: dict[str, dict[str, int]] = {}
    findings: list[dict[str, Any]] = []
    timings: list[float] = []
    for case in selected:
        want = case["expect"]
        started = time.perf_counter()
        got = deterministic_outcome(case)
        timings.append((time.perf_counter() - started) * 1000)
        row = by_kind.setdefault(want["kind"], {"cases": 0, "without_model": 0, "correct": 0})
        row["cases"] += 1
        problems: list[str] = []
        wrong: list[str] = []
        alone = not got["planner"]
        if want["kind"] == "not_edit":
            if got["edits"]:
                false_edits += 1
                wrong.append(f"edited {got['edits']} on a request that must not edit")
            ok = not got["edits"]
        elif want["kind"] == "unsupported":
            if got["edits"]:
                false_edits += 1
                wrong.append(f"edited {got['edits']} on an unsupported request")
            ok = not got["edits"] and "unsupported" in got["kinds"] and (
                not want.get("capability") or want["capability"] in got["capabilities"]
            )
            if not ok:
                problems.append(f"expected a refusal ({want.get('capability')}), got {got}")
        elif want["kind"] == "no_plan":
            ok = not got["edits"]
            if got["edits"]:
                false_edits += 1
                wrong.append(f"edited {got['edits']} where no tool fits")
        else:
            gold = [gold_card(s, cards) for s in plan_steps(case)]
            wanted = [c.op for c in gold if c is not None]
            ok = alone and bool(wanted) and all(op in got["edits"] for op in wanted)
            misread = wrong_reads(case, got, cards) if wanted else []
            if not wanted and got["edits"]:
                # a family's request (not registered yet) read as one of today's edits
                misread = [f"made {got['edits']} for a request of a tool not registered yet"]
            if misread:
                # an edit applied at once that is not what was asked: the worst outcome
                wrong_total += 1
                wrong += misread
                ok = False
            elif alone and not ok:
                problems.append(f"read as {got['edits'] or got['kinds']}, expected {wanted}")
        handled += 1 if alone else 0
        row["without_model"] += 1 if alone else 0
        correct += 1 if ok and alone else 0
        row["correct"] += 1 if ok and alone else 0
        if problems or wrong:
            findings.append(
                {"id": case["id"], "message": case["message"], "wrong": wrong, "problems": problems}
            )
    total = len(selected)
    report: dict[str, Any] = {
        "mode": "planner-coverage",
        "cases": total,
        "handled_without_model": handled,
        "coverage": round(handled / total, 4) if total else 0.0,
        "correct_without_model": correct,
        "false_edits": false_edits,
        "wrong_edits": wrong_total,
        "by_kind": by_kind,
        "latency_ms": {"p50": _percentile(timings, 0.5), "p95": _percentile(timings, 0.95)},
        "findings": findings,
    }
    report["gates"] = {
        "no_false_edits": false_edits == 0,
        "no_wrong_op_on_off_or_number": wrong_total == 0,
    }
    report["passed"] = all(report["gates"].values())
    return report


NEGATIVE_SHARE = 0.25  # of a live sample: requests nothing may be planned for


def sample_live(
    cases: list[dict[str, Any]], size: int, cards: tuple[ToolCard, ...]
) -> list[dict[str, Any]]:
    """What the live run asks the real model: plan cases the deterministic tier leaves to it
    (those it reads alone never reach the model in the chat) and, for a quarter of the
    sample, requests nothing may be planned for (no_plan, not_edit, unsupported). In file
    order, every n-th, so two runs ask the same things; topped up with cases the parser reads
    when too few are left to the model."""
    negatives_wanted = max(1, round(size * NEGATIVE_SHARE)) if size > 1 else 0
    runnable = [
        c
        for c in cases
        if c["expect"]["kind"] == "plan" and available(c, cards) and not c.get("blocked_by")
    ]
    left = [c for c in runnable if deterministic_outcome(c)["planner"]]
    read = [c for c in runnable if c not in left]
    negatives = [c for c in cases if c["expect"]["kind"] != "plan"]

    def every_nth(pool: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
        if count <= 0 or not pool:
            return []
        if len(pool) <= count:
            return list(pool)
        step = len(pool) / count
        return [pool[int(i * step)] for i in range(count)]

    wanted = size - negatives_wanted
    chosen = every_nth(left, wanted)
    chosen += every_nth(read, wanted - len(chosen))
    chosen += every_nth(negatives, size - len(chosen))
    order = {c["id"]: i for i, c in enumerate(cases)}
    return sorted(chosen, key=lambda c: order[c["id"]])


def all_live(cases: list[dict[str, Any]], cards: tuple[ToolCard, ...]) -> list[dict[str, Any]]:
    """Everything the chat can send the model: every runnable plan case the deterministic tier
    leaves to it, and every request nothing may be planned for, in file order (no sampling)."""
    runnable = [
        c
        for c in cases
        if c["expect"]["kind"] == "plan" and available(c, cards) and not c.get("blocked_by")
    ]
    left = {c["id"] for c in runnable if deterministic_outcome(c)["planner"]}
    return [c for c in cases if c["id"] in left or c["expect"]["kind"] != "plan"]


def sample(
    cases: list[dict[str, Any]], size: int, cards: tuple[ToolCard, ...]
) -> list[dict[str, Any]]:
    """A fixed sample across families: every ``len/size``-th runnable plan case, in file order,
    so two runs of the live script ask the same things."""
    runnable = [c for c in cases if c["expect"]["kind"] == "plan" and available(c, cards)]
    if len(runnable) <= size:
        return runnable
    step = len(runnable) / size
    return [runnable[int(i * step)] for i in range(size)]
