"""The local model as a planner over the tool cards (edit_tools), for what the parser cannot read.

The hand-built parser (edit_parser) reads most requests with no model. What it leaves (the
``unparsed`` pieces of edit_intents.ParseResult) comes here, in three tiers:

1. **Retrieval without the model** (``retrieve``): every registered card is scored by its cue
   phrases, its example requests (weighted so that words many cards share count little), its
   summary, the capability the request names and the selected visual's type. Names of visuals,
   fields and pages are blanked first, so "Avg MTD % change by Agents" is not a request to change
   anything. The best 3 to 6 cards go on.
2. **Cards without the model** (``card_clause``, called by the parser): every registered card is
   ranked; when a card of the families added after the parser (not ``edit_tools.BUILTIN_MODULE``)
   clearly leads, the request does not say not to, does not remove what the card adds, names no
   field the card has no slot for, and its words give every slot it needs (a named or selected
   visual, a field named in full, one word of a choice, one number, one color, a quoted text, a
   typed formula), it becomes that card's operation with no model call. A bool slot is set only
   when every distinctive word of its name (or one of its ``words``) is in the request ("row
   subtotals", not "rows" for values on rows); a color or a text that several slots could take
   goes to the one named next to it; a page defaults to the one on screen only for a card that
   places something or for "this page"; a visual named once fills one slot. A built-in card in the
   lead,
   a card and its removing sibling as the best two, or anything less clear is left to the model.
3. **The model chooses** (``EditPlanner.plan``): one call gets the short list of cards (op,
   summary, slots with their kinds and choices, at most one example each) and a compact context
   (the page's visuals by number with type, caption and place; pages; fields by relevance) within
   ``PROMPT_BUDGET`` bytes, and returns ``{kind, steps: [{tool, slots}]}`` under a JSON schema
   built for that call: visual, page and field slots can only be numbers from the lists, choices
   only listed words, numbers within the slot's bounds. Up to ``MAX_STEPS`` steps, one per change
   asked. ``unclear`` and ``unsupported`` are always allowed answers.

Nothing the model returns is applied as it is. **Every value must be backed by the request's
words, the selection, or an explicit question** (``Evidence``, ``_judge``): the visual, page and
field the request names win over the model's (the selection when it names none); colors and
numbers are the request's own (``find_colors``, ``find_amounts``, "second", "to the front");
choices and bools take the words' own evidence (the card tier's ``_choice_in``/``_bool_value``);
a text must be the request's own words (symbols as typed: "€", "0.0%") and not what it says it
does not want. A value the request does not talk about is left out and the step says so; one it
says differently sinks the step (the operation's default never stands in for it); a required one
the words do not give is asked. At most one step per piece of the request; a delete or clear
only when a removal word governs the card's own object; nothing that removes what it adds, and
nothing the request says not to do. Then the card builds the operation and
``report_edits.parse_operations`` must accept it. A ``dax`` or ``m`` slot the user did not type is
drafted in a separate short call (only when required, or when a change verb governs "formula" or
"DAX") with only the field and measure names, checked (DAX: the local parser, the model's names,
no self-reference; M: the Power Query validator), fed back once with the check's error, and
otherwise asked for. Every model here is the local one: a typed DAX formula and, for a changed
measure, its current formula reach it (nothing reaches an outside service); typed Power Query
is replaced where it stands (``m_prompt_text``). The chat shows
the steps as a plan (event ``edit_plan``) and applies them only on Apply, as one revision,
through the same port as any edit (the editor's dry run, its questions, receipts, Undo).
Every list the prompt
shows is numbered from 1 with no gaps.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import UnionType
from typing import (
    Annotated,
    Any,
    ClassVar,
    Literal,
    Protocol,
    TypeVar,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel, Field

from ai_powerbi_analyst.contracts import ClosedModel

from . import dax_patterns, edit_tools
from .chat_context import m_prompt_text
from .edit_capabilities import (
    card_capability,
    detect_capability,
    detect_unsupported,
    unsupported_keys,
)
from .edit_context import (
    EditContext,
    PageEntry,
    VisualEntry,
    describe_position,
    hue_family,
    type_name,
)
from .edit_intents import page_visuals, parse_edit_message, scope_page, visual_relevance
from .edit_model_grounding import (
    ground_group_targets,
    ground_relationship,
    ground_role_filter,
    relationship_setting,
    validate_role_expression,
)
from .edit_ops import match_fields
from .edit_parser import Parser
from .edit_semantic_constraints import constrain_edit_request
from .edit_targets import (
    candidate_label,
    find_page,
    find_pages_by_name,
    find_types,
    resolve_visuals,
)
from .edit_text import (
    BASE_COLORS,
    ColorMatch,
    SentenceKind,
    classify,
    clean,
    describes_calculation,
    find_amounts,
    find_colors,
    new_measure_calculation,
    plain_number,
    quoted,
    scaled,
    split_clause_parts,
    split_sentences,
    tokens,
)
from .edit_tools import Slot, ToolCard
from .edit_types import Choice, Clause, ParseKind, Target
from .pbir_document import DocumentError

LOGGER = logging.getLogger(__name__)

PROMPT_BUDGET = 3000  # bytes of system prompt plus user prompt, the 4B runtime's input budget
MODEL_SECONDS = 60  # one choose call (and one draft call) at most
TURN_SECONDS = 150  # every model call of one request together
MAX_STEPS = 4
MAX_CARDS = 6
MIN_CARDS = 3
MAX_REQUEST = 400  # characters of the request that go into the prompt
_RELATIONSHIP_OPS = {"create_relationship", "update_relationship", "delete_relationship"}
CHOOSE_TOKENS = 256
DRAFT_TOKENS = 220
# A card is offered only above this score, and the card tier needs a cue phrase this strong
# (a phrase of two words or the capability the request names) and a clear lead over the next.
MIN_SCORE = 1.0
STRONG_CUE = 3.5
LEAD = 2.0
RELATIVE_FLOOR = 0.25  # past the first MIN_CARDS, a card must score a quarter of the best one
# Points for a card of the capability the request names by its pattern ("data bars"): enough to
# lift it, not so much that every card of a big family crowds out the one the words fit.
NAMED_CAPABILITY = 6.0

_M = TypeVar("_M", bound=BaseModel)


class PlannerModel(Protocol):
    """The guarded local model, as much of it as the planner uses (``generate`` releases the
    runtime itself on a timeout or a failure)."""

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[_M],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> _M: ...


# --- what the model returns -----------------------------------------------------------------

SlotValue = str | int | float | bool | list[int] | None


class PlanStepAnswer(ClosedModel):
    tool: int = Field(ge=1, le=MAX_CARDS)
    slots: dict[str, SlotValue] = Field(default_factory=dict)


class PlanAnswer(ClosedModel):
    """The model's choice. The JSON schema the runtime constrains it with is built per call
    (``answer_model``): this class only checks the shape."""

    worked_examples: ClassVar[bool] = False  # no retrieved examples: they would break the budget

    kind: Literal["plan", "unclear", "unsupported"]
    steps: list[PlanStepAnswer] = Field(default_factory=list, max_length=MAX_STEPS)


class DraftAnswer(ClosedModel):
    worked_examples: ClassVar[bool] = False

    expression: str = Field(min_length=1, max_length=1500)


def answer_model(schema: Mapping[str, Any]) -> type[PlanAnswer]:
    """A ``PlanAnswer`` whose JSON schema is ``schema``: the runtime turns it into the grammar the
    model is decoded with, so the model can only write numbers from this call's lists."""
    frozen = json.dumps(schema)

    class ToolChoice(PlanAnswer):
        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            value: dict[str, Any] = json.loads(frozen)
            return value

    return ToolChoice


# --- the result -----------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanStep:
    """One operation of a plan, validated, with the line the plan card shows."""

    op: str
    operation: dict[str, Any]
    summary: str
    needs_desktop: bool = False
    confirm: bool = False
    source: Literal["cards", "model", "parser"] = "model"  # parser: read with no card at all
    notes: tuple[str, ...] = ()
    slots: Mapping[str, Any] = field(default_factory=dict)  # the values the card was filled with


@dataclass(frozen=True)
class PlanOutcome:
    """``plan``: steps to preview; ``clarify``/``unsupported``: a clause to show instead;
    ``failed``: nothing usable (the chat offers suggestion chips)."""

    kind: Literal["plan", "clarify", "unsupported", "failed"]
    steps: tuple[PlanStep, ...] = ()
    clause: Clause | None = None
    left_out: tuple[str, ...] = ()  # parts of the message the plan does not cover
    notes: tuple[str, ...] = ()  # what the plan card should say besides the steps
    reason: str = ""  # failed: why, for the log (never shown as it is)
    calls: int = 0
    prompt_bytes: int = 0

    @property
    def ops(self) -> tuple[dict[str, Any], ...]:
        return tuple(step.operation for step in self.steps)

    @property
    def needs_desktop(self) -> bool:
        return any(step.needs_desktop for step in self.steps)


# --- words ----------------------------------------------------------------------------------

_STOP = frozenset(
    re.findall(
        r"\w+",
        "the a an of and or to in on for by with per at it its this that these those my our me "
        "please can could would you i we be is are was as from into onto than then also so just "
        "all each every some any",
    )
)
_REMOVAL = re.compile(
    r"\b(?:delete|remove|drop|erase|clear|get\s+rid\s+of|take\b.{0,40}?\b(?:off|out|away)|"
    r"discard|reset|unfilter|no\s+longer|without)\b",
    re.I,
)
_OFF_WORDS = re.compile(
    r"\b(?:remove|hide|off|disable|no|not|without|stop|clear|turn\s+off|switch\s+off|don'?t|"
    r"never|invisible|hidden|get\s+rid\s+of|delete|erase|lose|ditch|"
    r"take\b[^,.;]{0,40}?\b(?:out|off|away))\b",
    re.I,
)
# ("on" is an on word only at the end of what is said: "data labels on", not "on this page")
_ON_WORDS = re.compile(
    r"\b(?:add|show|enable|display|turn\s+on|switch\s+on|put|include|label|give|with|want|"
    r"need|bring\s+back|unhide|visible)\b|\bon(?=\s*(?:$|[,.;!?]|\band\b|\bplease\b))",
    re.I,
)
# "drop shadow" is a shadow, not a request to drop one.
_DROP_SHADOW = re.compile(r"\bdrop[\s-]+shadows?\b", re.I)
# A request not to do something ("don't sync the StoreName slicer"): no card reads it without
# the model, and the model's plan for it is refused.
_NEGATION = re.compile(
    r"^\s*(?:please\s+)?(?:not|never)\b|\b(?:don'?t|do\s+not|does\s+not|doesn'?t|"
    r"shouldn'?t|should\s+not|must\s+not|mustn'?t|no\s+need\s+to)\b",
    re.I,
)


def _plain(text: str) -> str:
    return _DROP_SHADOW.sub("shadow", text)


def _content(text: str) -> list[str]:
    return [t for t in tokens(text) if t not in _STOP]


def _close(a: str, b: str) -> bool:
    """The same word, or one typo apart (two for a long word): "legnd" is the legend."""
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 2:
        return False
    limit = 2 if min(len(a), len(b)) >= 8 else 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1] <= limit


def mask_names(text: str, context: EditContext) -> str:
    """The request with the full names of visuals, fields and pages blanked: they say which
    thing to change, not what to do to it ("Avg MTD % change by Agents" asks for no change)."""
    spans: list[tuple[int, int]] = []
    visible = [v for v in context.visuals if not v.is_group]
    for entry in visible:
        for name in (entry.caption, *entry.fields):
            spans += _name_spans(text, name)
    for page in context.pages:
        spans += _name_spans(text, page.name)
    for hit in match_fields(text, context):
        spans.append((hit.start, hit.end))
    chars = list(text)
    for start, end in spans:
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def _name_spans(text: str, name: str) -> list[tuple[int, int]]:
    words = name.split()
    if not words or len(name) < 3:
        return []
    pattern = r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in words) + r"(?![\w-])"
    return [(m.start(), m.end()) for m in re.finditer(pattern, text, re.I)]


# --- retrieval ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Scored:
    card: ToolCard
    score: float
    cue: float  # the part of the score that came from cue phrases and the named capability


@dataclass(frozen=True)
class _Cue:
    words: tuple[str, ...]  # every word, in order ("make it a")
    content: tuple[str, ...]  # without the small words ("make")


@dataclass(frozen=True)
class _Entry:
    cues: tuple[_Cue, ...]
    examples: tuple[frozenset[str], ...]
    summary: frozenset[str]
    capability: str
    has_visual: bool


def _without_names(request: str) -> str:
    """An example request without its names: they are written as the report spells them
    (capitals, digits: "Total MTD Sales", "StoreName", "Q3") and belong to the example's report,
    not to the tool; "store" must not make a filter request look like a swap."""
    return " ".join(w for w in request.split() if not re.search(r"[A-Z0-9#]", w))


# Words that give a value, not a tool: a color, a unit. "teal" in an example of a button card
# says nothing about the button when a request asks for teal columns.
_SHADES = ("dark", "light", "pale", "bright", "deep", "soft")
_VALUE_WORDS = frozenset(
    {t for w in (*BASE_COLORS, *_SHADES) for t in tokens(w)}
    | {"pt", "px", "pixel", "point", "percent", "k", "m"}
)


# Words an example shares with too many requests to say which tool it is: a wish ("I want",
# "should"), a verb every card uses ("set", "make", "put") and the kind of visual it targets.
_GENERIC_EXAMPLE_WORDS = frozenset(
    {
        "should",
        "want",
        "need",
        "like",
        "would",
        "set",
        "make",
        "put",
        "get",
        "let",
        "please",
        "everything",
        "anything",
        "something",
        "new",
        "now",
        "slicer",
        "chart",
        "card",
        "visual",
        "graph",
        "kpi",
    }
)


def _tool_words(words: set[str] | frozenset[str]) -> set[str]:
    return {
        w
        for w in words
        if w not in _VALUE_WORDS and w not in _GENERIC_EXAMPLE_WORDS and not w.isdigit()
    }


class _Index:
    """What retrieval knows about a set of cards, worked out once per set."""

    def __init__(self, cards: Sequence[ToolCard]) -> None:
        self.entries: list[_Entry] = []
        documents: list[set[str]] = []
        for card in cards:
            cues = tuple(
                _Cue(tuple(tokens(cue)), tuple(_content(cue))) for cue in card.cues if _content(cue)
            )
            examples = tuple(
                frozenset(_tool_words(set(_content(_without_names(request)))))
                for request, _ in card.examples
            )
            summary = frozenset(_content(card.summary)) | frozenset(_content(card.op))
            self.entries.append(
                _Entry(
                    cues,
                    examples,
                    summary,
                    card_capability(card),
                    any(s.kind in {"visual", "visuals"} for s in card.slots),
                )
            )
            documents.append(
                {w for e in examples for w in e} | {w for c in cues for w in c.content}
            )
        total = max(len(cards), 1)
        frequency: dict[str, int] = {}
        for document in documents:
            for word in document:
                frequency[word] = frequency.get(word, 0) + 1
        self.weight = {w: math.log(1 + total / n) for w, n in frequency.items()}


_INDEXES: dict[tuple[int, ...], tuple[tuple[ToolCard, ...], _Index]] = {}


def _index(cards: tuple[ToolCard, ...]) -> _Index:
    """The index of a set of cards (cards hold dicts, so they are keyed by identity; the cards
    are kept with their index so an id is never reused while it is cached)."""
    key = tuple(id(card) for card in cards)
    found = _INDEXES.get(key)
    if found is not None:
        return found[1]
    made = _Index(cards)
    if len(_INDEXES) >= 16:
        _INDEXES.pop(next(iter(_INDEXES)))
    _INDEXES[key] = (cards, made)
    return made


def _cue_score(entry: _Entry, sequence: Sequence[str], words: Sequence[str]) -> float:
    """Points for the card's cue phrases in the request: a phrase in its order (small words
    included, so "make it a" is not every "make"), else all of its content words."""
    score = 0.0
    for cue in entry.cues:
        if len(cue.words) == 1:
            if any(_close(cue.words[0], w) for w in words):
                score += 2.0
            continue
        if _in_order(cue.words, sequence):
            score += 1.5 * len(cue.content) + 1.0
        elif len(cue.content) > 1 and all(any(_close(c, w) for w in words) for c in cue.content):
            score += 0.75 * len(cue.content)
    return score


def _in_order(cue: Sequence[str], words: Sequence[str]) -> bool:
    """The cue's words in the request in order, at most two other words between each."""
    for start, word in enumerate(words):
        if not _close(cue[0], word):
            continue
        at, ok = start, True
        for wanted in cue[1:]:
            window = words[at + 1 : at + 4]
            step = next((i for i, w in enumerate(window) if _close(wanted, w)), None)
            if step is None:
                ok = False
                break
            at += step + 1
        if ok:
            return True
    return False


def _overlap(example: frozenset[str], words: set[str], index: _Index) -> float:
    """How much an example says the request is its tool's: the weights of the tool words they
    share, a single shared word counting for little ("top" of a filter and of a mobile layout)."""
    shared = example & words
    weight = sum(index.weight.get(w, 0.0) for w in shared)
    return weight if len(shared) > 1 else 0.4 * weight


_TABLE_TYPES = ("tableEx", "pivotTable")
_TABLE_CARD = re.compile(
    r"^(?:table\s+or\s+matrix\s+layout|format\s+one\s+table|move\s+a\s+table\s+column)", re.I
)


def visual_types_of(card: ToolCard) -> tuple[str, ...]:
    """The visual types a card's visual slot fits: its own ``visual_types``, else a table and a
    matrix for a card whose summary says it formats a table ("Table or matrix layout"), else
    any (a family card without them: see the report's card requests)."""
    if card.visual_types:
        return card.visual_types
    return _TABLE_TYPES if _TABLE_CARD.search(card.summary) else ()


def _target_type(text: str, context: EditContext) -> str | None:
    """The type of the visual the request is about: the selected one, else the one it names."""
    if context.selection and (selected := context.visual(context.selection)) is not None:
        return selected.visual_type
    resolution = resolve_visuals(text, context)
    if resolution.ok and resolution.explicit and resolution.how in {"caption", "field", "type"}:
        kinds = {entry.visual_type for entry in resolution.entries}
        return kinds.pop() if len(kinds) == 1 else None
    return None


# Where a change is meant to happen: in the data model ("rename the column in the model") or in
# one visual ("rename the Rank column header in the table"). A model card and a report card
# often share their words ("call the column"); these decide between them.
_MODEL_SCOPE = re.compile(
    r"\b(?:in|from|of|to)\s+(?:the\s+)?(?:data\s+)?model\b|\bdata\s+model\b|"
    r"\b(?:in|from|of)\s+the\s+(?:data(?:set)?|source|database)\b|\beverywhere\b|"
    r"\bin\s+(?:all|every)\s+(?:the\s+)?(?:visuals?|reports?|pages?)\b",
    re.I,
)
_VISUAL_SCOPE = re.compile(
    r"\b(?:column\s+)?(?:headers?|headings?)\b|\b(?:in|on|of|for)\s+(?:the|this|that)\s+"
    r"(?:table|matrix|chart|graph|visual|card|slicer)\b|\bfor\s+this\s+visual\b",
    re.I,
)
SCOPE_POINTS = 4.0


def retrieve(
    text: str,
    context: EditContext,
    cards: Sequence[ToolCard],
    *,
    limit: int = MAX_CARDS,
) -> list[Scored]:
    """The cards a request most likely asks for, best first (at most ``limit``, each above
    ``MIN_SCORE``). No model call."""
    if not cards:
        return []
    index = _index(tuple(cards))
    masked = mask_names(text, context)
    sequence = tokens(masked)
    words = [w for w in sequence if w not in _STOP]
    unique = set(words)
    wanted = detect_capability(text)
    named = wanted.key if wanted is not None and wanted.status == "supported" else None
    if (
        named == "drillthrough_tooltip_pages"
        and re.search(r"\btool\s*tips?\b", masked, re.I)
        and not re.search(r"\bpage\b", masked, re.I)
        and match_fields(text, context)
    ):
        named = "fields"
    if named is None and re.search(
        r"\b(?:filter|exclude|limit|restrict|keep\s+only|only\s+(?:show|keep))\b", masked, re.I
    ):
        named = "filters"
    target = _target_type(text, context)
    in_model = _MODEL_SCOPE.search(text) is not None
    in_visual = not in_model and _VISUAL_SCOPE.search(text) is not None
    found: list[tuple[float, int, Scored]] = []
    for position, (card, entry) in enumerate(zip(cards, index.entries, strict=True)):
        cue = _cue_score(entry, sequence, words)
        if named is not None and entry.capability == named:
            cue += NAMED_CAPABILITY
        best = max((_overlap(example, unique, index) for example in entry.examples), default=0.0)
        score = cue + min(best, 6.0) + 0.5 * min(3, len(entry.summary & unique))
        fits = visual_types_of(card)
        if target is not None and entry.has_visual and fits:
            score += 1.0 if target in fits else -3.0
        if in_model:
            score += SCOPE_POINTS if card.needs_desktop else -SCOPE_POINTS
        elif in_visual and card.needs_desktop:
            score -= SCOPE_POINTS
        if score > MIN_SCORE:
            found.append((score, position, Scored(card, round(score, 3), round(cue, 3))))
    found.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in found[:limit]]


# --- the cards the planner uses -----------------------------------------------------------------


def available_cards() -> tuple[ToolCard, ...]:
    """Every card the planner may offer (registered operation, not opted out). A family module
    that fails to import is logged and left out; the built-in cards always remain."""
    try:
        return edit_tools.planner_cards()
    except Exception:  # noqa: BLE001 - one broken family must not stop the others
        LOGGER.exception("Could not read the tool cards of every family")
        return edit_tools.planner_cards((edit_tools.BUILTIN_MODULE,))


def family_cards() -> tuple[ToolCard, ...]:
    """The cards of the families added after the parser: the only ones the card tier reads."""
    try:
        builtin = edit_tools.builtin_ops()
    except Exception:  # noqa: BLE001 - see available_cards
        LOGGER.exception("Could not read the built-in tool cards")
        return ()
    return tuple(card for card in available_cards() if card.op not in builtin)


# --- slot values ------------------------------------------------------------------------------

_FIELD_KINDS = frozenset({"field", "fields", "measure", "column"})


def _field_value(kind: str, table: str, name: str, target: str) -> Any:
    """A field as the operation field takes it: the name alone where that field is text
    (``update_measure.name``, a family's ``measure: str``), else ``{table, name}``."""
    if target == "str":
        return name
    return {"table": table, "name": name}


def _flat(annotation: Any) -> list[Any]:
    """The types an annotation allows: unions and Optional opened, a list's items for a list."""
    origin = get_origin(annotation)
    if origin is Annotated:
        return _flat(get_args(annotation)[0])
    if origin in (Union, UnionType):
        return [t for arg in get_args(annotation) for t in _flat(arg)]
    if origin in (list, tuple, set, frozenset):
        return [t for arg in get_args(annotation) if arg is not Ellipsis for t in _flat(arg)]
    return [annotation]


def _target_shape(card: ToolCard, slot: Slot) -> str:
    """Whether the operation field a slot lands in (``Slot.field``, else its name) takes text
    ("str") or a field reference ("object"), read from the operation's own class, registered
    or not yet. When the slot is not a field of the class (a card with a ``build``), the
    documented default (edit_tools.Slot): a measure's name, a column's ``{table, name}``."""
    cls = edit_tools.operation_classes().get(card.op)
    fallback = "str" if slot.kind == "measure" else "object"
    if cls is None:
        return fallback
    model: Any = cls
    for part in (slot.field or slot.name).split("."):
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict) or part not in fields:
            return fallback
        allowed = [t for t in _flat(fields[part].annotation) if t is not type(None)]
        models = [t for t in allowed if isinstance(t, type) and hasattr(t, "model_fields")]
        # a nested operation field ("filter.field"): walk into the model that has the next part
        model = models[0] if models else (allowed[0] if allowed else None)
    allowed = [t for t in _flat(model) if t is not type(None)]
    if any(isinstance(t, type) and (hasattr(t, "model_fields") or t is dict) for t in allowed):
        return "object"
    if any(get_origin(t) is dict for t in allowed):
        return "object"
    return "str" if str in allowed else fallback


@dataclass
class _Lists:
    """The numbered lists of one prompt: what a number in the model's answer means. Every list
    is numbered from 1 with no gaps: item ``n`` is ``list[n - 1]``."""

    page: PageEntry | None = None
    visuals: list[VisualEntry] = field(default_factory=list)  # the visuals shown, by number
    omitted: int = 0  # visuals of the page left out of the list
    on_page: list[VisualEntry] = field(default_factory=list)  # every visual of the page
    pages: list[PageEntry] = field(default_factory=list)
    fields: list[tuple[str, str, str]] = field(default_factory=list)  # (kind, table, name)
    tables: list[str] = field(default_factory=list)

    def number(self, entry: VisualEntry) -> int | None:
        """The number a visual has in this prompt's list (None: not listed)."""
        for n, listed in enumerate(self.visuals, 1):
            if listed.id == entry.id:
                return n
        return None


def _nth(items: Sequence[Any], value: Any) -> Any:
    """Item ``value`` (1-based) of a numbered list, or None for anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return items[value - 1] if 1 <= value <= len(items) else None


def _field_label(kind: str, table: str, name: str) -> str:
    return f"[{name}]" if kind == "measure" else f"{table}[{name}]"


def _visual_label(entry: VisualEntry, context: EditContext | None = None) -> str:
    """How a step names a visual: its label, and its page when the report has more than one."""
    if context is None or len(context.pages) <= 1:
        return entry.label
    page = context.page(entry.page_id)
    return f"{entry.label} on page '{page.name}'" if page is not None else entry.label


def _page_label(page: PageEntry) -> str:
    return f"page '{page.name}'"


# --- the card tier (no model) -------------------------------------------------------------------

_AMBIGUOUS = object()
_CALLED = re.compile(
    r"\b(?:called|named|titled|saying|that\s+says|to\s+say|reading)\s+(?P<text>.+?)\s*[.!]?$",
    re.I,
)


def typed_expression(text: str, kind: str = "dax") -> str | None:
    """A formula the user typed in the request: in backticks, or after "=" (DAX), or a
    ``let ... in`` block (M)."""
    fenced = re.search(r"`([^`]{2,})`", text)
    if fenced:
        return fenced.group(1).strip()
    if kind == "m":
        block = re.search(r"\blet\b.+\bin\b.+$", text, re.I | re.S)
        return block.group(0).strip() if block else None
    after = re.search(r"(?<![<>=!])=(?!=)\s*(?P<expr>.+)$", text, re.S) or re.search(
        r"\b(?:to|as)\s+(?P<expr>(?:[A-Za-z][A-Za-z0-9.]*\(|\[|').+)$", text, re.S
    )
    if after and re.search(r"[\[(]", after.group("expr")):
        expression = re.sub(
            r"\s+(?:formatted|with\s+(?:the\s+)?format)\s+as\s+\S+\s*$", "", after.group("expr")
        )
        return expression.strip().rstrip(".") or None
    return None


def _polarity(text: str) -> bool | None:
    text = _plain(text)
    if _OFF_WORDS.search(text):
        return False
    if _ON_WORDS.search(text):
        return True
    return None


def _last_polarity(text: str) -> bool | None:
    """The on/off of the last on or off word ("delete the chart, give it a shadow": on)."""
    text = _plain(text)
    off = [m.start() for m in _OFF_WORDS.finditer(text)]
    on = [m.start() for m in _ON_WORDS.finditer(text)]
    if not off and not on:
        return None
    return max(on, default=-1) > max(off, default=-1)


# Words of a bool slot's name that say nothing about what it switches.
_GENERIC_BOOL_WORDS = frozenset(
    {"show", "hide", "hidden", "on", "off", "is", "use", "apply", "to", "true", "false", "set"}
)
# Bool slots that switch the card's own thing ("show" of a header card is the header) and those
# that say the opposite ("hidden"). A slot named "remove_..." or "exclude" is true when the
# request removes or leaves out what it names.
_SHOW_SLOTS = frozenset({"show", "visible", "enabled", "on"})
_HIDE_SLOTS = frozenset({"hidden", "off", "hide"})
_INVERTED = ("remove_", "hide_", "no_", "exclude", "clear_")


def _phrase_pattern(phrase: str) -> str:
    """A slot word or a choice as the request may write it: "data_labels" is "data labels",
    "TopCenter" is "top center", and a last word may be plural or not."""
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", phrase)
    parts = [p for p in re.split(r"[\s_]+", spaced) if p]
    if not parts:
        return r"(?!)"
    body = [re.escape(p) for p in parts[:-1]]
    last = parts[-1]
    stem = last[:-1] if len(last) > 3 and last.casefold().endswith("s") else last
    body.append(re.escape(stem) + (r"(?:s|es)?" if stem[-1:].isalpha() else ""))
    return r"(?<![\w-])" + r"[\s_-]+".join(body) + r"(?![\w-])"


def _slot_at(slot: Slot, text: str) -> int | None:
    """Where the request names what a slot sets: one of the slot's ``words``, or EVERY
    distinctive word of its name (of its description when the name has none): "row subtotals"
    for ``row_subtotals``, not "rows" alone (that is ``values_on_rows`` too). None when it does
    not name it."""
    for phrase in slot.words:
        found = re.search(_phrase_pattern(phrase), text, re.I)
        if found:
            return found.start()
    words = {w for w in tokens(slot.name.replace("_", " ")) if w not in _GENERIC_BOOL_WORDS}
    if not words:
        words = set(_content(slot.description)) - _GENERIC_BOOL_WORDS
    if not words:
        return None
    first: dict[str, int] = {}
    for match in re.finditer(r"[A-Za-z0-9']+", text):
        for token in tokens(match.group(0)):
            if token in words:
                first.setdefault(token, match.start())
    if not words <= set(first):
        return None
    return min(first.values())


# The words that switch a thing on or off outright (not "give" or "put", which say where or
# what, not whether): what a show or hidden slot takes.
_SWITCH_ON = re.compile(
    r"\b(?:add|show|enable|display|turn\s+on|switch\s+on|include|label|bring\s+back|unhide|"
    r"visible)\b|\bon(?=\s*(?:$|[,.;!?]|\band\b|\bplease\b))"
    # "put value labels on the chart" adds them; "put the legend on top" only moves it
    r"|\bput\b(?!(?:\s+\S+){0,5}?\s+(?:top|bottom|left|right|under|above|below|beside|next|"
    r"front|behind|back)\b)",
    re.I,
)


def _governed_polarity(card: ToolCard, slot: Slot, text: str) -> bool | None:
    """The on/off a show or hidden slot takes: for a card that changes a part of a visual (the
    title, the legend), an on or off word within four words of what the card works on ("add
    data labels" shows no title; "give the chart the title X" shows nothing); for a card that
    shows or hides the visual or page itself, the request's on or off."""
    targets = {"visual", "visuals", "page"}
    others = [s for s in card.slots if s.kind not in targets and s is not slot]
    if not others:
        return _polarity(text)
    own = _object_words(card)
    words = [(m.start(), m.end(), m.group(0)) for m in re.finditer(r"[A-Za-z0-9']+", text)]
    for pattern, value in ((_OFF_WORDS, False), (_SWITCH_ON, True)):
        for match in pattern.finditer(text):
            at = next((i for i, w in enumerate(words) if w[1] > match.start()), len(words))
            near = words[max(0, at - 4) : at] + words[at : at + 5]
            if {t for w in near for t in tokens(w[2])} & own:
                return value
    return None


def _bool_value(slot: Slot, text: str, card: ToolCard | None = None) -> bool | None:
    """A bool slot from the words: show/visible/on take the request's on/off polarity (and
    hidden/off the opposite), unless another bool slot of the card names what the words
    switch ("hide the axis title" hides the title, not the axis); any other slot takes the
    on/off nearest before its words ("turn on the row subtotals", "without the total row").
    Unset when the request does not name it, so a word the request did not say never
    switches anything: "add data bars" leaves bar_only alone."""
    text = _plain(text)
    if card is not None and card.op in _RELATIONSHIP_OPS:
        said = relationship_setting(slot.name, text)
        return said if isinstance(said, bool) else None
    if slot.name in _SHOW_SLOTS | _HIDE_SLOTS:
        if card is not None and any(
            other is not slot
            and other.kind == "bool"
            and other.name not in _SHOW_SLOTS | _HIDE_SLOTS
            and _slot_at(other, text) is not None
            for other in card.slots
        ):
            return None
        overall = _polarity(text) if card is None else _governed_polarity(card, slot, text)
        if overall is None:
            return None
        return overall if slot.name in _SHOW_SLOTS else not overall
    at = _slot_at(slot, text)
    if at is None:
        return None
    # the on/off word nearest before it ("show the legend but hide the title"), else anywhere
    near = _last_polarity(" ".join(re.findall(r"[A-Za-z0-9']+", text[:at])[-5:]))
    if near is None:
        near = _polarity(text)
    if slot.name.startswith(_INVERTED):
        return True if near is None else not near
    return True if near is None else near


# Words of a slot's name that do not tell two slots of one kind apart ("font_color",
# "background_color": the color is the value).
_GENERIC_SLOT_WORDS = frozenset({"color", "colour", "text", "value", "name", "the", "of", "to"})


def _slot_words(slot: Slot) -> set[str]:
    return {w for w in tokens(slot.name.replace("_", " ")) if w not in _GENERIC_SLOT_WORDS}


def _near(text: str, start: int, end: int, slot: Slot) -> bool:
    """Whether a distinctive word of the slot's name is next to the value (within three words
    before or after it): "a light grey background" puts the grey in ``background``."""
    before = re.findall(r"[A-Za-z0-9']+", text[:start])[-3:]
    after = re.findall(r"[A-Za-z0-9']+", text[end:])[:3]
    near = {t for w in (*before, *after) for t in tokens(w)}
    return bool(_slot_words(slot) & near)


def _one_of_many(slot: Slot, rivals: Sequence[Slot], text: str, start: int, end: int) -> object:
    """A value that fits several slots of one kind goes to the one whose words are next to it
    (True: this slot; None: another one); when none or more than one is, the words do not say
    which (``_AMBIGUOUS``: the model fills it)."""
    if len(rivals) <= 1:
        return True
    near = [s for s in rivals if _near(text, start, end, s)]
    if len(near) == 1:
        return True if near[0] is slot else None
    return _AMBIGUOUS


# Other words a request uses for a choice ("under the chart" is the bottom). Keyed by the
# choice as words, lower case; shared by every card with that choice.
_CHOICE_WORDS: dict[str, str] = {
    "bottom": r"under(?:neath)?|below|beneath|bottom",
    "top": r"above|top",
    "left": r"left",
    "right": r"right",
    "center": r"cent(?:er|re)d?|middle",
    # "center it" is across the page; up and down has to be said
    "center h": r"cent(?:er|re)d?(?!\s+vertical)|horizontal(?:ly)?\s+cent(?:er|re)d?|"
    r"(?:middle|cent(?:er|re))\b.{0,30}\bhorizontally",
    "center v": r"cent(?:er|re)d?\s+vertically|vertical(?:ly)?\s+cent(?:er|re)d?|"
    r"(?:middle|cent(?:er|re))\b.{0,30}\bvertically",
    "ascending": r"ascending|(?:lowest|smallest|least)\s+(?:to\s+(?:highest|largest|biggest)|"
    r"first)|a\s*(?:to|-)\s*z|low\s+to\s+high|increasing|alphabetical(?:ly)?",
    "descending": r"descending|(?:highest|largest|biggest|most)\s+(?:to\s+(?:lowest|smallest)|"
    r"first)|z\s*(?:to|-)\s*a|high\s+to\s+low|decreasing",
    "front": r"front|on\s+top\s+of\s+(?:everything|the\s+others|all\s+the\s+others|all)|"
    r"top\s+layer",
    "back": r"back|behind(?:\s+everything)?|bottom\s+layer",
    "forward": r"forward|up\s+one\s+layer",
    "backward": r"backwards?|down\s+one\s+layer",
    "add": r"add|put|include|also|plus|insert|show",
    "remove": r"remove|delete|drop|take\s+(?:\w+\s+){0,3}?(?:out|off|away)|get\s+rid\s+of|"
    r"without",
    "replace": r"replace|instead|swap|only",
    "horizontal": r"horizontal(?:ly)?|across|side\s+by\s+side|in\s+a\s+row|left\s+to\s+right",
    "vertical": r"vertical(?:ly)?|down|stack(?:ed)?|top\s+to\s+bottom|in\s+a\s+column",
    # (horizontal gridlines are the value axis's, vertical ones the category axis's)
    "x": r"x|categor(?:y|ies)|vertical\s+grid\s*lines?",
    "y": r"y|values?\s+axis|horizontal\s+grid\s*lines?",
    "x axis": r"x[\s-]*axis|horizontal\s+axis|category\s+(?:axis|labels)",
    "y axis": r"y[\s-]*axis|vertical\s+axis|value\s+axis",
    "data labels": r"(?:data|value)\s+labels?|labels?\s+on\s+the\s+(?:bars|columns|points)",
    # ("by value" says how, not which values)
    "values": r"(?<!by\s)(?<!its\s)(?<!their\s)(?<!the\s)values|numbers|figures",
    "headers": r"(?:column\s+)?headers?",
    "title": r"titles?|headings?|captions?",
    "legend": r"legends?|key",
    "category": r"categor(?:y|ies)|x[\s-]*axis",
    "series": r"series|legend",
    "tooltips": r"tool\s*tips?",
    "thousands": r"thousands?|k",
    "millions": r"millions?|m",
    "billions": r"billions?|bn",
    "none": r"none|no\s+units|full\s+numbers?|exact\s+numbers?|"
    r"stop\b.{0,40}\bfrom\s+(?:filtering|highlighting)|(?:should\s+)?not\s+(?:filter|highlight)|"
    r"no\s+interactions?",
    "running sum": r"running\s+(?:sum|total)s?|cumulative\s+(?:sum|total)",
    "gradient": r"gradients?|colou?r\s+scales?|heat\s*maps?|shad(?:e|ing)|"
    r"by\s+(?:its\s+|their\s+|the\s+)?values?|from\s+\S+\s+to\s+\S+",
    "rules": r"rules?|whenever|when\b.{0,40}\b(?:below|above|under|over|greater|less|more|"
    r"negative|positive|zero|\d)|"
    # a color on each side of one number: "red below 0 and green from 0 up"
    r"\bbelow\s+-?[$€£]?\d[^,.;]{0,40}?\b(?:from|above|over)\s+-?[$€£]?\d",
    # (the bars of a chart; "data bars" are a style of their own, never its colors)
    "data colors": r"data\s+colou?rs?|(?<!data\s)(?:bars|slices|lines|points)",
    "font color": r"(?:font|text)\s+colou?rs?|colou?r\s+of\s+the\s+(?:text|numbers|values)",
    "moving average": r"moving\s+averages?|rolling\s+averages?",
    "auto": r"auto(?:matic(?:ally)?)?",
    "greater than": r"greater\s+than|more\s+than|above|over|exceeds?|>",
    "greater or equal": r"at\s+least|or\s+more|>=|greater\s+than\s+or\s+equal(?:\s+to)?",
    "less than": r"less\s+than|fewer\s+than|below|under|<",
    "less or equal": r"at\s+most|or\s+less|<=|less\s+than\s+or\s+equal(?:\s+to)?",
    "is not": r"is\s+not|isn'?t|not\s+equal(?:\s+to)?|other\s+than",
    "contains": r"contains?|containing",
    "is blank": r"blanks?|empty|missing",
    "oval": r"ovals?|circles?|ellipses?",
    "rectangle": r"rectangles?|squares?|boxe?s?",
    "rounded rectangle": r"rounded\s+rectangles?|pills?",
    "line": r"lines?|dividers?",
    "page": r"pages?|canvas",
    "selection": r"each\s+other|one\s+another|together|selection",
    "report": r"report|everywhere|all\s+(?:the\s+)?pages|whole",
    "visual": r"visuals?|charts?|graphs?|tables?|matri(?:x|ces)|cards?|slicers?",
    "left of": r"left\s+of|to\s+the\s+left|before",
    "right of": r"right\s+of|to\s+the\s+right|after|next\s+to|beside",
    "above": r"above|over|on\s+top\s+of",
    "below": r"below|under(?:neath)?|beneath",
    "align with": r"align(?:ed)?\s+with|line\s+(?:it\s+)?up\s+with|same\s+(?:row|line)",
    "canvas": r"canvas|page\s+background",
    "wallpaper": r"wallpaper|outside\s+the\s+page|behind\s+the\s+page",
}


_COUNT_WORDS = {"three": "3", "four": "4", "five": "5"}


def _choice_pattern(choice: str) -> str:
    """A choice as the request may say it: its own words, or the other words for it. A choice
    that starts with a count ("ThreeColoredArrows") may be said with the digit ("3 colored
    arrows")."""
    own = _phrase_pattern(choice)
    key = " ".join(re.split(r"[\s_]+", re.sub(r"(?<=[a-z])(?=[A-Z])", " ", choice))).casefold()
    first, _, rest = key.partition(" ")
    if first in _COUNT_WORDS and rest:
        own = f"(?:{own}|{_phrase_pattern(_COUNT_WORDS[first] + ' ' + rest)})"
    extra = _CHOICE_WORDS.get(key)
    return own if extra is None else rf"(?:{own}|(?<![\w-])(?:{extra})(?![\w-]))"


def _choice_in(slot: Slot, text: str) -> object:
    found = [c for c in slot.choices if re.search(_choice_pattern(c), text, re.I)]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        longest = max(found, key=len)
        others = [
            c
            for c in found
            if c != longest
            and c.casefold().replace("_", " ") not in longest.casefold().replace("_", " ")
        ]
        return _AMBIGUOUS if others else longest
    return None


# The kinds of icon a request names by their noun (an icon set's name has it inside:
# "ThreeColoredArrows", "ThreeTrafficLightsRimmed"). Common words that name other things too
# ("shapes", "circles", "boxes", "pie charts") are left to the full set name.
_ICON_NOUNS = {
    "arrow": r"arrows?",
    "flag": r"flags?",
    "star": r"stars?",
    "triangle": r"triangles?",
    "symbol": r"symbols?",
    "trafficlight": r"traffic\s+lights?",
    "signalmeter": r"signal\s+meters?",
}
_ICON_REFINERS = (
    (r"\bcolou?r(?:ed|ful)?\b", "colored"),
    (r"\bgr[ae]y(?:ed)?\b", "gray"),
    (r"\b(?:three|3)\b", "three"),
    (r"\b(?:four|4)\b", "four"),
    (r"\b(?:five|5)\b", "five"),
)


def _icon_kinds(card: ToolCard, text: str) -> list[str]:
    """The icon sets the words name: one said in full ("three colored arrows", "3 colored
    arrows"), else the sets of the icon noun they say ("arrows"), narrowed by the count and
    color words they also say ("colored arrows": the colored arrow sets)."""
    icons = card.slot("icon_set")
    if icons is None:
        return []
    # A quoted column/table/page after a location word names the target. Keep a
    # quoted preset after "use"/"show" available as the requested icon choice.
    chars = list(text)
    for start, end, _ in quoted(text):
        if re.search(
            r"\b(?:on|in|to|for)\s+(?:the\s+|this\s+|that\s+)?"
            r"(?:(?:column|table|matrix|visual|page)\s+)?$",
            text[:start],
            re.I,
        ):
            chars[start:end] = " " * (end - start)
    text = "".join(chars)
    exact = _choice_in(icons, text)
    canonical = [
        name
        for name in icons.choices
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.I)
    ]
    if canonical:
        if isinstance(exact, str) and exact not in canonical:
            canonical.append(exact)
        return canonical
    if isinstance(exact, str):
        return [exact]
    nouns = [k for k, pattern in _ICON_NOUNS.items() if re.search(rf"\b{pattern}\b", text, re.I)]
    kinds = [
        c
        for c in icons.choices
        if any(key in c.casefold().replace("grayed", "gray") for key in nouns)
    ]
    for pattern, word in _ICON_REFINERS:
        if re.search(pattern, text, re.I):
            narrowed = [c for c in kinds if word in c.casefold()]
            kinds = narrowed or kinds
    return kinds


def _conditional_style(card: ToolCard, text: str) -> object:
    """The conditional style the words give. A named kind of icon ("three colored arrows",
    "arrow icons") gives the icons style; it outranks the rules style that only a condition
    implies ("... when they are below 0": the icons are shown by value), and is ambiguous next
    to another style said by name ("data bars")."""
    style = card.slot("style")
    chosen = _choice_in(style, text) if style is not None else None
    if card.op != "set_conditional_format" or not _icon_kinds(card, text):
        return chosen
    if chosen is None or chosen == "icons":
        return "icons"
    if chosen == "rules" and not re.search(r"\brules?\b", text, re.I):
        return "icons"
    return _AMBIGUOUS


# Where icons go (left/right of the value, top/middle/bottom of the cell): a position word
# counts for them unless it says where something else is ("the left side of the page") or is a
# condition ("below 0", "above the target").
_ICON_PLACES = frozenset({"icon_position", "icon_alignment"})
_NOT_AN_ICON_PLACE = re.compile(
    r"^\s*(?:-?\s*[$€£]?\d|zero\b|nothing\b|(?:the\s+)?(?:average|target|threshold|goal|"
    r"limit|mean|median)\b|(?:hand\s+)?(?:side\s+|edge\s+|corner\s+|half\s+|part\s+)?of\s+"
    r"(?:the\s+|this\s+|that\s+)?(?:page|table|chart|visual|report|screen|canvas|column|"
    r"matrix|card|slicer|dashboard|header|title)s?\b|(?:hand\s+)?side\b|"
    # ("the right icons", "the right kind": right as in correct)
    r"(?:colou?red\s+|kind\s+of\s+)?(?:icons?|arrows?|sets?|kinds?|types?|ones?|choices?)\b)",
    re.I,
)


def _icon_place(slot: Slot, text: str) -> object:
    """The icon position or alignment the words give, as ``_choice_in`` but only from a word
    that places the icons."""
    found: list[str] = []
    for choice in slot.choices:
        for match in re.finditer(_choice_pattern(choice), text, re.I):
            if not _NOT_AN_ICON_PLACE.match(text[match.end() :]):
                found.append(choice)
                break
    if len(found) == 1:
        return found[0]
    return _AMBIGUOUS if found else None


# No decimals said in words: the number of decimal places is 0.
_NO_DECIMALS = re.compile(r"\b(?:no|zero|without)\s+decimals?\b|\bwhole\s+numbers?\b", re.I)


def _word_value(
    card: ToolCard,
    slot: Slot,
    text: str,
    context: EditContext,
    resolution: Any,
    counts: Mapping[str, int],
) -> object:
    """The value a slot takes from the words alone, None when they do not give it, or
    ``_AMBIGUOUS`` when they give more than one."""
    kind = slot.kind
    masked = str(resolution.masked or text)
    if kind == "visual":
        if counts.get("visual", 0) > 1:
            return _AMBIGUOUS
        if not resolution.ok or len(resolution.entries) != 1:
            return _AMBIGUOUS if resolution.ambiguous else None
        entry = resolution.entries[0]
        # named, selected, or the one named earlier in the message ("... to that chart")
        if not (resolution.explicit or resolution.how in {"selection", "carry"}):
            return None
        fits = visual_types_of(card)
        if fits and entry.visual_type not in fits:
            return _AMBIGUOUS
        return entry.id
    if kind == "visuals":
        if not resolution.ok or not (
            resolution.explicit or resolution.how in {"selection", "carry"}
        ):
            return None
        return [e.id for e in resolution.entries]
    if kind == "page":
        if counts.get("page", 0) > 1:
            return _AMBIGUOUS  # where it goes and where it points: the words do not say which
        page = named_page(text, context)
        if page is None and _page_by_default(card, text):
            page = _default_page(context)  # the page it goes on: the one on screen
        return page.id if page is not None else None
    if kind in _FIELD_KINDS:
        # from the words as said: the resolver blanks a field that also named the visual
        # ("sort the table by Rank" names the table by its Rank column, and sorts by it)
        hits = [
            h for h in match_fields(text, context) if kind in {"field", "fields"} or h.kind == kind
        ]
        if kind == "fields":
            return [{"table": h.table, "name": h.name} for h in hits] or None
        if counts.get("field", 0) > 1 and hits:
            return _AMBIGUOUS
        if len(hits) > 1:
            return _AMBIGUOUS
        if not hits:
            return None
        hit = hits[0]
        # a column several tables have is the one of the table the words name, or asked
        bound = column_table(hit.name, text, context) if hit.kind == "column" else hit.table
        if bound is None:
            return _AMBIGUOUS
        return _field_value(kind, bound, hit.name, _target_shape(card, slot))
    if kind == "table":
        named = [t for t in context.tables if _name_spans(text, t)]
        return named[0] if len(named) == 1 else (_AMBIGUOUS if named else None)
    if kind == "enum":
        if card.op in _RELATIONSHIP_OPS:
            return relationship_setting(slot.name, mask_names(text, context))
        if card.op == "set_conditional_format" and slot.name == "style":
            return _conditional_style(card, masked)
        return _choice_in(slot, masked)
    if kind == "bool":
        return _bool_value(slot, masked, card)
    if kind in {"number", "integer"}:
        if slot.name == "decimals" and _NO_DECIMALS.search(masked) and not find_amounts(masked):
            return 0  # "no decimals", "whole numbers"
        if counts.get("number", 0) > 1:
            amounts = find_amounts(masked)
            return _AMBIGUOUS if amounts else None
        amounts = find_amounts(masked)
        if len(amounts) != 1:
            return _AMBIGUOUS if amounts else None
        rivals = [s for s in card.slots if s.kind in {"number", "integer"}]
        named_settings = [s for s in rivals if _talked_about(s, masked)]
        if named_settings and slot not in named_settings:
            return None
        value = scaled(amounts[0])  # "at 10M" is 10,000,000
        if kind == "integer":
            if not float(value).is_integer():
                return _AMBIGUOUS
            return _bounded(slot, int(value))
        return _bounded(slot, value)
    if kind == "color":
        colors = find_colors(masked, context.palette)
        if card.op == "set_conditional_format":
            if slot.name in {"below_color", "above_color"} and _RULE_COMPARISON.search(masked):
                rule_color = _conditional_rule_color(slot, masked, colors)
                return rule_color.hex if isinstance(rule_color, ColorMatch) else rule_color
            endpoint = _conditional_endpoint_color(slot, masked, colors)
            if endpoint is not None:
                return endpoint.hex if isinstance(endpoint, ColorMatch) else endpoint
        if (
            len(colors) == 2
            and re.fullmatch(r"\s*to\s*", masked[colors[0].end : colors[1].start])
            and (
                re.search(r"\bfrom\s*$", masked[: colors[0].start])
                or re.search(r"\bgradient\b", masked, re.I)
            )
        ):
            # "from white to green": the scale's low end and its high end, nothing else
            if re.match(r"(?:min|from|start|low|lowest|first)", slot.name):
                return colors[0].hex
            if re.match(r"(?:max|to|end|high|highest|last)", slot.name):
                return colors[1].hex
            return None
        if len(colors) != 1:
            return _AMBIGUOUS if colors else None
        # one color and several color slots: the one named next to it, else the model's call
        rivals = [s for s in card.slots if s.kind == "color"]
        mine = _one_of_many(slot, rivals, masked, colors[0].start, colors[0].end)
        return colors[0].hex if mine is True else mine
    if kind == "text":
        if card.op == "set_field_display_name" and slot.name == "name":
            renamed = _column_label(text)
            if renamed is not None:
                return renamed
        if card.op == "create_measure" and slot.name == "name":
            definition = new_measure_calculation(text)
            if definition is not None:
                return definition[0]
        if re.search(r"field|column", slot.name):
            # a field named by its words ("the Rank column") or quoted, as the header reads
            hits = match_fields(text, context)
            if len(hits) == 1:
                return hits[0].name
            quotes = [inner for _, _, inner in quoted(text) if _is_field_name(inner, context)]
            if not hits and len(quotes) == 1:
                return quotes[0].strip()
            if hits:
                return _AMBIGUOUS
            # a field as a visual shows it ("MTD Sales" in the agents table), the longest said
            # (of the visuals the words could mean, when they narrow it to a few: never an
            # alias of a visual they do not mean)
            candidates = [e for e in getattr(resolution, "candidates", ()) or ()]
            visuals = resolution.entries if resolution.ok else candidates or context.visuals
            shown = {
                name
                for v in visuals
                for name in v.fields
                if len(name) >= 3 and _name_spans(text, name)
            }
            shown = {n for n in shown if not any(n != m and n in m for m in shown)}
            if len(shown) == 1:
                return shown.pop()
            if (
                not shown
                and card.op == "set_visual_calculation"
                and resolution.ok
                and _calculation_subject(text) is None
            ):
                measures_bound = {
                    name
                    for entry in resolution.entries
                    for kind, _table, name in entry.bindings
                    if kind == "measure"
                }
                if len(measures_bound) == 1:
                    # "a moving average on the Total MTD Sales chart": the chart's only value.
                    # Never when the words name what it is of ("a running total of discounts"):
                    # a subject the chart does not show is asked, not swapped for its value.
                    return measures_bound.pop()
            return _AMBIGUOUS if shown else None
        rivals = [
            s for s in card.slots if s.kind == "text" and not re.search("field|column", s.name)
        ]
        found = quoted(text)
        names_a_field = any(re.search("field|column", s.name) for s in card.slots)
        if len(found) == 1:
            start, end, inner = found[0]
            if names_a_field and _is_field_name(inner, context):
                return None  # "add data bars to 'MTD Sales'": the quote is the field, no text
            mine = _one_of_many(slot, rivals, text, start, end)
            return (inner.strip() or None) if mine is True else mine
        called = _CALLED.search(text)
        if called is None or found:
            return None
        return called.group("text").strip(" '\"") if len(rivals) <= 1 else _AMBIGUOUS
    if kind in {"dax", "m"}:
        return typed_expression(text, kind)
    return None


# What a calculation is worked out on, as said: "a running total of discounts", "the share of
# Profit", "a moving average of the values".
_CALCULATION_OF = re.compile(
    r"\b(?:total|sum|average|avg|mean|share|percent(?:age)?|change|difference|rank(?:ing)?|"
    r"calculation|count)s?\s+(?:of|for|on)\s+(?P<what>[^,.;!?]+?)"
    r"(?=\s+(?:to|on|in|into|onto|for|as|at|across|by|over|per|within)\b|[,.;!?]|$)",
    re.I,
)
_GENERIC_SUBJECT = frozenset(
    tokens(
        "it its them their this that the value values number numbers bar bars column columns "
        "line lines point points data measure chart graph visual table matrix"
    )
)


def _calculation_subject(text: str) -> str | None:
    """The field a visual calculation's words say it is of, or None when they name none
    past words that point at the visual itself ("of it", "of the values")."""
    for match in _CALCULATION_OF.finditer(text):
        what = match.group("what").strip()
        said = set(tokens(what))
        if said and not said <= _GENERIC_SUBJECT and not re.fullmatch(r"\d+\s*\w*", what):
            return what
    return None


def tables_with_column(name: str, context: EditContext) -> list[str]:
    """Every table that has a column of this name (StoreKey is in FactSales and DimStore)."""
    wanted = name.strip().casefold()
    return [t for t, cols in context.tables.items() if wanted in {c.casefold() for c in cols}]


def column_table(name: str, text: str, context: EditContext) -> str | None:
    """The table a column the request names belongs to: its only table, else the one of its
    tables the request names ("the StoreKey column of DimStore", "DimStore[StoreKey]",
    "DimStore's StoreKey", "the StoreKey column in DimStore"); None when several tables have
    it and the words name none of them (the question lists them)."""
    tables = tables_with_column(name, context)
    if len(tables) <= 1:
        return tables[0] if tables else None
    said = said_tables(name, text, context)
    return said[0] if len(said) == 1 else None


def said_tables(name: str, text: str, context: EditContext) -> list[str]:
    """The tables with a column of this name that the request names ("a relationship between
    FactSales StoreKey and DimStore StoreKey" names both)."""
    return [
        t
        for t in tables_with_column(name, context)
        if _name_spans(text, t)
        or re.search(rf"'?{re.escape(t)}'?\s*\[\s*{re.escape(name)}\s*\]", text, re.I)
    ]


def _is_field_name(text: str, context: EditContext) -> bool:
    wanted = text.strip().casefold()
    names = [m.name for m in context.measures] + [c for cs in context.tables.values() for c in cs]
    names += [f for v in context.visuals for f in v.fields]
    return any(name.casefold() == wanted for name in names)


def _bounded(slot: Slot, value: float) -> object:
    if slot.minimum is not None and value < slot.minimum:
        return _AMBIGUOUS
    if slot.maximum is not None and value > slot.maximum:
        return _AMBIGUOUS
    return value


# A slot whose value means nothing without another one: a visual calculation's template is
# worked out on a field ("a running total" of what?). Filled alone it is a guess.
_PARTNERS = {"template": "field"}


def _lacks_its_partner(card: ToolCard, values: Mapping[str, Any]) -> Slot | None:
    """The slot a filled slot needs and the step lacks, or None."""
    for name, partner in _PARTNERS.items():
        needed = card.slot(partner)
        if values.get(name) is not None and needed is not None and values.get(partner) is None:
            return needed
    return None


def _default_page(context: EditContext) -> PageEntry | None:
    if context.selection:
        selected = context.visual(context.selection)
        if selected is not None:
            return context.page(selected.page_id)
    if context.active_page and context.page(context.active_page):
        return context.page(context.active_page)
    return context.pages[0] if len(context.pages) == 1 else None


# "this page", "the page", "the current page": the page on screen, said.
_THIS_PAGE = re.compile(
    r"\b(?:this|the|current|same|active)\s+(?:current\s+|active\s+)?(?:page|canvas)\b"
    r"(?!\s+(?:tooltip|navigator|type))",
    re.I,
)
# A page named by its bare name counts only as "the Forecast page", "page Forecast" or after a
# word that places something on or points at a page ("on Forecast", "to the Forecast"): "add a
# forecast line" names no page.
_BEFORE_PAGE_NAME = re.compile(
    r"\b(?:on|to|onto|into|in|from|of|at|for|page|tab|called|named)\s+(?:the\s+)?$", re.I
)
_AFTER_PAGE_NAME = re.compile(r"^\s*(?:page|tab)s?\b", re.I)


def _bare_page_spans(text: str, context: EditContext) -> list[tuple[int, int]]:
    """Where the request says a page's name without saying it is a page."""
    spans: list[tuple[int, int]] = []
    for page in context.pages:
        name = page.name.strip()
        if len(name) < 2 or name.isdigit():
            continue
        for start, end in _name_spans(text, name):
            if _BEFORE_PAGE_NAME.search(text[:start]) or _AFTER_PAGE_NAME.match(text[end:]):
                continue
            if text[start:end] == name and start > 0 and name[:1].isupper():
                continue  # written as the page is ("make Product Mix the last page")
            spans.append((start, end))
    return spans


def page_words(text: str, context: EditContext) -> str:
    """The request with the page names it does not say are pages blanked out, so "add a
    forecast line to the chart" does not move the planner to the Forecast page."""
    chars = list(text)
    for start, end in _bare_page_spans(text, context):
        for i in range(start, min(end, len(chars))):
            chars[i] = " "
    return "".join(chars)


def named_page(text: str, context: EditContext) -> PageEntry | None:
    """The page the request names ("page 2", "the Forecast page", "on Forecast", "this
    page"), not a page name used as a word."""
    plain = page_words(text, context)
    by_name = find_pages_by_name(plain, context)
    if len(by_name) == 1:
        return by_name[0]  # a page named by its name over "the last page" as a place
    page, _ = find_page(plain, context)
    return page


def _placement(card: ToolCard) -> bool:
    """A card that puts something new on a page (a text box, a shape, a button, a visual): the
    page it goes on is the one on screen unless the request names another."""
    return card.op.startswith("add_") and not any(
        s.kind in {"visual", "visuals"} and s.required for s in card.slots
    )


def _page_by_default(card: ToolCard, text: str) -> bool:
    """Whether the page on screen may fill the card's page slot: for a card that places
    something, or when the request says "this page". Never for a card that changes what kind
    of page a page is, or what a visual points at ("use a report page tooltip on the chart"
    turns no page into a tooltip page)."""
    return _placement(card) or _THIS_PAGE.search(text) is not None


def _sync_source_text(text: str) -> str:
    """A sync destination names where a copy goes, while its source stays on screen."""
    if re.search(r"\bsync\b", text, re.I) and re.search(r"\bslicers?\b", text, re.I):
        return re.split(r"\b(?:to|with|across)\b", text, maxsplit=1, flags=re.I)[0].strip()
    return text


def _sync_visuals(text: str, context: EditContext) -> list[VisualEntry]:
    """A slicer's displayed caption outranks another slicer's shared field binding: "the
    Channel slicer" is the one captioned ChannelName (or Channel Name, Channel ID...)."""
    if not (re.search(r"\bsync\b", text, re.I) and re.search(r"\bslicers?\b", text, re.I)):
        return []
    source_text = _sync_source_text(text)
    page = scope_page(context, page_words(source_text, context))
    return [
        entry
        for entry in context.visuals
        if entry.visual_type == "slicer"
        and (page is None or entry.page_id == page.id)
        and any(_name_spans(source_text, form) for form in _label_forms(entry.caption))
    ]


# The part of a field's name that says what it is, past the suffix that says how it is stored:
# ChannelName is "channel", StoreKey "store", Product_Code "product".
_STORAGE_SUFFIX = re.compile(r"(?<=[a-z0-9])[\s_-]*(?:Name|Key|ID|Id|Code|Desc|Description|Label)$")


def _label_forms(name: str) -> list[str]:
    """The ways a request may say a field or caption: as written, split at its capitals
    ("Channel Name"), and without its storage suffix ("Channel")."""
    name = name.strip()
    forms = [name, re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", name)]
    base = _STORAGE_SUFFIX.sub("", name).strip(" _-")
    if base != name and len(base) >= 3:
        forms += [base, re.sub(r"(?<=[a-z0-9])(?=[A-Z])|_", " ", base)]
    return [f for f in dict.fromkeys(" ".join(f.split()) for f in forms) if len(f) >= 3]


def _field_name_of(value: Any) -> str:
    """The name of a field slot's value (a {table, name} reference, or its name)."""
    if isinstance(value, Mapping):
        return str(value.get("name") or "")
    if isinstance(value, list) and value:
        return _field_name_of(value[0])
    return str(value or "")


def _known_names(context: EditContext) -> set[str]:
    """Every name the report's model and pages have (pages, visual captions, tables,
    measures, columns), folded. (A header's display alias, "Store" for StoreName, is how a
    visual shows a field, not a name a filter value could be mistaken for.)"""
    names = {p.name for p in context.pages} | {v.caption for v in context.visuals}
    names |= set(context.tables) | {m.name for m in context.measures}
    names |= {c for cols in context.tables.values() for c in cols}
    return {n.casefold() for n in names if n}


# Words that stand before a filter's field without being one of its values ("Filter Online
# channel", "Keep Store channel", "Only Online channel").
_NOT_A_VALUE = frozenset(
    {
        "filter", "filters", "limit", "restrict", "keep", "leave", "exclude", "excluding",
        "except", "show", "showing", "only", "just", "set", "make", "remove", "hide", "drop",
        "include", "including", "without", "all", "every", "each", "no", "not", "our", "my",
        "your", "their", "its", "this", "that", "these", "those", "a", "an", "to", "for", "on",
        "in", "of", "from", "with", "by", "i", "we", "please", "then", "but",
    }
)  # fmt: skip
# "every page", "all the pages", "each of the other pages", "everywhere"
_ALL_PAGES = re.compile(
    r"\b(?:all|every|each)(?:\s+(?:of\s+)?(?:the|my|our|other|report's))*\s+(?:other\s+)?"
    r"pages?\b|\beverywhere\b|\bacross\s+the\s+(?:whole\s+)?report\b",
    re.I,
)
# A synced slicer kept out of sight: "but keep it hidden there", "hide it on Forecast".
_HIDDEN_THERE = re.compile(
    r"\b(?:hid(?:e|den|ing)|invisible|not\s+(?:be\s+)?(?:visible|shown|showing))\b", re.I
)
_THERE = re.compile(
    r"\b(?:there|those\s+pages?|that\s+page|these\s+pages?|on\s+them|on\s+it|on\s+each|"
    r"(?:the\s+)?destinations?|(?:the\s+)?copies)\b",
    re.I,
)
# Words that point at the selected visual ("this", "these", "the selected chart").
_SELECTION_REF = re.compile(
    r"\b(?:this|these|those)\b(?!\s+(?:page|report|bookmark|tab|measure|column|field)s?\b)"
    r"|\bselect(?:ed|ion)\b",
    re.I,
)


# A page a button points at: after a verb of going ("goes to", "takes you back to", "opens",
# "links to"); a page it is put on: after "on", "onto", "in" (and "to" or "into" when nothing
# says the button goes anywhere, or it is a back button: "add a back button to Forecast").
_GOES_TO = re.compile(
    r"\b(?:go(?:es|ing)?|went|takes?|taking|navigat\w*|links?|linking|jumps?|jumping|opens?|"
    r"opening|points?|pointing|leads?|leading|sends?|sending|brings?|bringing|drills?|"
    r"switch(?:es)?|moves?)\b(?:\s+\w+){0,3}?\s+(?:to|at|through\s+to)\s+(?:the\s+)?"
    r"(?:page\s+)?$"
    r"|\b(?:opens?|opening)\s+(?:the\s+)?(?:page\s+)?$"
    r"|\b(?:destination|target)\s+(?:page\s+)?(?:is\s+|of\s+)?(?:the\s+)?(?:page\s+)?$",
    re.I,
)
_PUT_ON = re.compile(r"\b(?:on|onto|in)\s+(?:the\s+)?(?:page\s+)?$", re.I)
_PUT_TO = re.compile(r"\b(?:to|into)\s+(?:the\s+)?(?:page\s+)?$", re.I)
# A back button ("a back button", "a button that takes people back", not "back to Forecast").
_BACK_BUTTON = re.compile(
    r"\bback(?:\s+arrow)?\s+buttons?\b|\bbuttons?\b[^.;]{0,40}?\b(?:go(?:es)?|takes?\s+\w+|"
    r"sends?\s+\w+|brings?\s+\w+)\s+back\b(?!\s+to\b)",
    re.I,
)
# A button that goes nowhere a page names: back, reset, clear all slicers...
_NO_DESTINATION = re.compile(
    rf"{_BACK_BUTTON.pattern}|\b(?:reset|clear\s+all\s+slicers|apply\s+all\s+slicers|"
    r"q\s*&\s*a|help|information|info)\s+buttons?\b",
    re.I,
)


def _button_pages(
    text: str, context: EditContext
) -> tuple[list[PageEntry], list[PageEntry], list[PageEntry]]:
    """The pages a button request names, as (put on, pointed at, either): "add a button to
    the Forecast page that goes to Product Mix" puts it on Forecast and points at Product Mix;
    "add a button to Forecast" says one or the other."""
    placed: list[PageEntry] = []
    targets: list[PageEntry] = []
    either: list[PageEntry] = []
    goes_nowhere = _NO_DESTINATION.search(text) is not None
    for page in context.pages:
        for start, _ in _name_spans(text, page.name):
            before = text[:start]
            if _GOES_TO.search(before):
                targets.append(page)
            elif _PUT_ON.search(before):
                placed.append(page)
            elif _PUT_TO.search(before):
                either.append(page)
    if either and (targets or goes_nowhere):
        placed += either  # the destination is said otherwise, or there is none
        either = []
    return placed, targets, either


def _placed_page(card: ToolCard, text: str, context: EditContext) -> PageEntry | None:
    """A button's placement is distinct from the page its action points at: the page the
    words put it on, else the one on screen when no page could be it; None when the words
    leave it open (the question lists the pages)."""
    if not (_placement(card) and card.slot("target_page") is not None):
        return named_page(text, context)
    placed, _targets, either = _button_pages(text, context)
    if placed:
        return placed[0] if len({p.id for p in placed}) == 1 else None
    return None if either else _default_page(context)


def _column_label(text: str) -> str | None:
    match = re.search(
        r"\bcall\s+(?:the\s+)?[^,;]+?\s+(?:column|field|header)\s+(.+?)"
        r"(?=\s+(?:in|on|of|for)\s+(?:the|this)\s+(?:table|matrix|chart|visual)\b|[.!]?$)",
        text,
        re.I,
    )
    return match.group(1).strip(" '\"") if match is not None else None


def _button_caption(value: str, text: str, context: EditContext) -> bool:
    if re.search(r"\b(?:text|label|caption|says|called|named|titled)\b", text, re.I):
        return True
    return any(
        inner.strip().casefold() == value.casefold()
        and not any(inner.strip().casefold() == p.name.casefold() for p in context.pages)
        for _, _, inner in quoted(text)
    )


def fill_from_words(
    card: ToolCard,
    text: str,
    context: EditContext,
    carry: Sequence[VisualEntry] = (),
) -> dict[str, Any] | None:
    """Every slot of ``card`` that the words give, or None when a required one is missing or
    the words give more than one value for a slot. A visual the request names once fills one
    slot only (not the ``source`` and the ``targets`` of a format painter)."""
    resolution = resolve_visuals(page_words(text, context), context, carry=list(carry))
    # an optional field slot the words do not name ("sort by" of a column's properties) takes
    # no field: the one field the request names is the column it is about
    slots = [
        s
        for s in card.slots
        if s.required or s.kind not in _FIELD_KINDS or _slot_at(s, text) is not None
    ]
    # the one number said goes to the number slot the words name ("2 px wide"), else to the
    # card's plain value ("a constant line at 10M"); the other number slots take none
    numeric = [s for s in slots if s.kind in {"number", "integer"}]
    if len(numeric) > 1:
        said = [s for s in numeric if s.required or _talked_about(s, text)]
        plain = [s for s in numeric if s.name in {"value", "amount", "number"}]
        keep = said or plain
        if keep:  # (none of them named: the number stays ambiguous, the model's to place)
            slots = [s for s in slots if s not in numeric or s in keep]
    counts: dict[str, int] = {}
    for slot in slots:
        group = (
            "field"
            if slot.kind in {"field", "measure", "column"}
            else "number"
            if slot.kind in {"number", "integer"}
            else slot.kind
        )
        counts[group] = counts.get(group, 0) + 1
    filled: dict[str, Any] = {}
    later: list[Slot] = []
    for slot in slots:
        value = _word_value(card, slot, text, context, resolution, counts)
        if value is _AMBIGUOUS:
            return None
        if value is None and slot.kind == "table":
            later.append(slot)  # the table of the column the words name, once that is known
            continue
        if value is None:
            if slot.required:
                return None
            continue
        filled[slot.name] = value
    for slot in later:
        value = _implied(card, slot, filled, context)
        if value is not None:
            filled[slot.name] = value
        elif slot.required:
            return None
    if _lacks_its_partner(card, filled) is not None:
        return None  # a template without the field it works on is the model's (or a question)
    used = {
        v for s in card.slots if s.kind == "visual" and isinstance(v := filled.get(s.name), str)
    }
    for slot in card.slots:
        if slot.kind == "visuals" and isinstance(filled.get(slot.name), list):
            rest = [v for v in filled[slot.name] if v not in used]
            if rest:
                filled[slot.name] = rest
            elif slot.required:
                return None
            else:
                del filled[slot.name]
    return filled


def card_clause(
    text: str,
    context: EditContext,
    carry: Sequence[VisualEntry] = (),
    *,
    cards: Sequence[ToolCard] | None = None,
) -> Clause | None:
    """The clause a family card makes of a request the parser could not read, with no model:
    only when one card's cue clearly leads among every card (a built-in card in the lead means
    the parser's own reading failed: the model's to read), the request does not say not to,
    does not remove what the card adds, and its words give every slot the card needs. A card
    and its sibling that removes the same thing, the two best, are left to the model."""
    if cards is None:
        pool = available_cards()
        try:
            builtin = edit_tools.builtin_ops()
        except Exception:  # noqa: BLE001 - see available_cards
            LOGGER.exception("Could not read the built-in tool cards")
            return None
    else:
        pool, builtin = tuple(cards), frozenset()
    if not pool or _NEGATION.search(text):
        return None
    ranked = [
        s for s in retrieve(text, context, pool, limit=4) if _asked_for(s.card, text, context)
    ]
    if not ranked or ranked[0].card.op in builtin:
        return None
    lead = ranked[0].score - ranked[1].score if len(ranked) > 1 else ranked[0].score
    # a strong cue phrase, or the capability the request names by its pattern ("shade the Rank
    # column from white to green") with a wide lead over the next card
    if ranked[0].cue < STRONG_CUE and not (ranked[0].cue >= NAMED_CAPABILITY and lead >= 2 * LEAD):
        return None
    if len(ranked) > 1 and (
        ranked[0].score - ranked[1].score < LEAD or _siblings(ranked[0].card, ranked[1].card)
    ):
        return None
    card = ranked[0].card
    if _leaves_a_field(card, text, context):
        return None
    filled = fill_from_words(card, text, context, carry)
    if filled is None:
        return _column_question(card, text, context, carry)
    grounded = ground_group_targets(context, text, card, filled)
    if isinstance(grounded, Clause):
        return grounded
    filled = grounded
    grounded = ground_role_filter(context, text, card, filled, typed=typed_expression(text, "dax"))
    if isinstance(grounded, Clause):
        return grounded
    filled = grounded
    grounded = ground_relationship(context, text, card, filled)
    if isinstance(grounded, Clause):
        return grounded
    filled = grounded
    constrained = constrain_edit_request(context, text, card, filled)
    if constrained.clarification is not None:
        if constrained.choices and (icon_slot := card.slot("icon_set")) is not None:
            options = [(choice.fill["icon_set"], choice.label) for choice in constrained.choices]
            question = _value_question(
                context,
                card,
                constrained.values,
                icon_slot,
                text,
                options,
                source="cards",
                question=constrained.clarification.question,
            )
            return question if isinstance(question, Clause) else constrained.clarification
        return constrained.clarification
    filled = constrained.values
    if _changes_nothing(card, filled):
        return None
    step = build_step(card, filled, context, source="cards", request=text)
    if isinstance(step, str):
        return None
    return step_clause(step, text)


def _column_question(
    card: ToolCard, text: str, context: EditContext, carry: Sequence[VisualEntry]
) -> Clause | None:
    """ "hide the StoreKey column in the model" when FactSales and DimStore both have one: a
    question with one ready step per table (the words fill everything else), never a guess."""
    if not any(s.kind in {"column", "field"} for s in card.slots):
        return None
    names = {h.name for h in match_fields(text, context) if h.kind == "column"}
    if len(names) != 1:
        return None
    name = names.pop()
    tables = tables_with_column(name, context)
    if len(tables) < 2 or column_table(name, text, context) is not None:
        return None
    choices: list[Choice] = []
    for table in tables:
        filled = fill_from_words(card, f"{text}, in {table}", context, carry)
        if filled is None:
            return None
        step = build_step(card, filled, context, source="cards")
        if isinstance(step, str):
            return None
        label = step.summary.rstrip(".") or f"{table}[{name}]"
        choices.append(Choice(label, ops=(step.operation,), summary=step.summary))
    listed = " and ".join(tables) if len(tables) == 2 else ", ".join(tables)
    return Clause(
        text,
        ParseKind.CLARIFY,
        question=f"{listed} each have a column {name}. Which one should I change?",
        choices=tuple(choices),
    )


def _value_question(
    context: EditContext,
    card: ToolCard,
    values: Mapping[str, Any],
    slot: Slot,
    text: str,
    options: Sequence[tuple[str, str]],
    *,
    source: Literal["cards", "model"],
    question: str | None = None,
) -> Clause | str:
    """Existing finite choices carry validated operations with all original slots."""
    choices = []
    for value, label in options[:6]:
        step = build_step(card, {**values, slot.name: value}, context, source=source)
        if isinstance(step, PlanStep):
            choices.append(Choice(label[:160], ops=(step.operation,), summary=step.summary))
    if not choices:
        return f"I need to know {slot.description}."
    return Clause(
        text,
        ParseKind.CLARIFY,
        question=question
        or (f"Which {slot.name.replace('_', ' ')}? ({card.label}: {slot.description})"),
        choices=tuple(choices),
    )


def _leaves_a_field(card: ToolCard, text: str, context: EditContext) -> bool:
    """Whether the request names a field the card has no slot for ("show Profit in the tooltip
    of the chart" is about Profit; a card that only points the chart at a tooltip page leaves
    it out). A field that is a visual's caption names the visual, not a field."""
    takes = any(
        s.kind in _FIELD_KINDS or (s.kind == "text" and re.search("field|column", s.name))
        for s in card.slots
    )
    if takes:
        return False
    captions = [span for v in context.visuals if v.caption for span in _name_spans(text, v.caption)]
    return any(
        not any(start <= hit.start and hit.end <= end for start, end in captions)
        for hit in match_fields(text, context)
    )


def step_clause(step: PlanStep, text: str) -> Clause:
    """A card-tier step as a parser clause: a model change waits for its confirmation, a card
    that wants a yes asks with one button, anything else applies with the rest of the message."""
    targets: tuple[Target, ...] = ()
    visual = step.operation.get("visual")
    if isinstance(visual, str):
        targets = (Target((visual,), None, "", "caption"),)
    if step.needs_desktop:
        return Clause(text, ParseKind.MODEL_EDIT, ops=(step.operation,), summary=step.summary)
    if step.confirm:
        apply = Choice(f"Apply: {step.summary}", ops=(step.operation,), summary=step.summary)
        return Clause(
            text,
            ParseKind.CLARIFY,
            question=f"This change needs your OK first: {step.summary}",
            choices=(apply,),
            targets=targets,
        )
    return Clause(
        text, ParseKind.APPLY, ops=(step.operation,), summary=step.summary, targets=targets
    )


# --- building and checking one step -----------------------------------------------------------


# --- what a removal word removes ----------------------------------------------------------

_REMOVAL_AT = re.compile(
    r"\btake\s+(?P<taken>(?!(?:off|out|away)\b)[^,.;!?]{1,40}?)\s+(?:off|out|away)\b"
    r"|\b(?:delete|remove|drop|erase|clear|get\s+rid\s+of|discard|reset|unfilter|lose|ditch|"
    r"scrap|trash|kill|toss|nuke|without|no\s+longer(?:\s+(?:show|display|need|want|use))?)\b",
    re.I,
)
_OFF_AT = re.compile(
    r"\b(?:turn|switch)\s+(?P<turned>(?!off\b)[^,.;!?]{1,40}?)\s+off\b"
    r"|\b(?:hide|disable|stop(?:\s+(?:showing|displaying|using|syncing))?|turn\s+off|"
    r"switch\s+off|no\s+more|no)\b",
    re.I,
)
# Where the thing a removal word removes ends: "remove the border | from the slicer".
_OBJECT_END = re.compile(
    r"\b(?:from|in|on|of|off|at|around|for|to|out|inside|within|across|and|but|then|so|"
    r"because|please)\b|[,.;:!?]",
    re.I,
)
# Parts of a visual, a request may remove instead of the visual itself.
_PART_NOUNS = re.compile(
    r"\b(?:borders?|frames?|outlines?|shadows?|backgrounds?|titles?|subtitles?|headings?|"
    r"captions?|legends?|labels?|axis|axes|grid\s*lines?|colou?rs?|fonts?|icons?|data\s+bars?|"
    r"totals?|subtotals?|markers?|zoom\s+sliders?|padding|search\s+box|links?|"
    r"small\s+multiples|banded\s+rows|rows|running\s+totals?|tool\s*tips?|alt\s+text|headers?|"
    r"sparklines?|lines?|filters?|formatting|corners?|transparency|spacing|values|numbers|"
    r"series|drill\w*|interactions?|sort(?:ing)?|bookmarks?|conditional|gradient|rules?|"
    r"calculations?|averages?|trends?|layout|wrap)\b",
    re.I,
)
_VISUAL_REFS = re.compile(
    r"\b(?:qzvisual|visuals?|charts?|graphs?|tables?|matri(?:x|ces)|slicers?|cards?|kpis?|"
    r"gauges?|maps?|textbox(?:es)?|images?|pictures?|logos?|buttons?|shapes?|tiles?|it|this|"
    r"that|them|these|those|everything)\b",
    re.I,
)
_FIELD_NOUNS = re.compile(r"\b(?:columns?|fields?|measures?)\b", re.I)
_VISUAL_COMPOUND = re.compile(
    r"\b(?:(?:stacked|clustered)\s+)?(?:bar|column|line|area|pie|donut|doughnut|scatter|combo|"
    r"ribbon|waterfall|funnel)\s+(?=charts?\b|graphs?\b)|\btext\s+box",
    re.I,
)
# Verbs of card names: what is left of the name is what the card works on.
_VERB_WORDS = frozenset(
    {
        "set", "add", "remove", "delete", "clear", "change", "create", "update", "rename",
        "move", "place", "sync", "unsync", "group", "ungroup", "copy", "duplicate", "make",
        "put", "show", "hide", "turn", "reset", "mark", "drill", "arrange", "align",
        "distribute", "swap", "apply", "use", "insert", "new", "refresh", "get", "take",
        "keep", "give", "switch", "stop", "off", "on", "the", "a", "an", "to", "of", "for",
        "in", "all", "any", "every", "visual", "chart", "graph", "report", "it", "this",
        "that", "when", "should", "not", "no", "more", "same", "one",
    }
)  # fmt: skip
_OBJECT_WORDS: dict[tuple[str, str], frozenset[str]] = {}


def _object_words(card: ToolCard) -> frozenset[str]:
    """What a card works on, as words: its name and cue phrases without their verbs
    ("set_conditional_format" and "data bars" give conditional, format, data, bar)."""
    key = (card.family, card.op)
    found = _OBJECT_WORDS.get(key)
    if found is None:
        words = set(tokens(card.op.replace("_", " ")))
        for cue in card.cues:
            words |= set(tokens(cue))
        found = frozenset(w for w in words if w not in _VERB_WORDS and not w.isdigit())
        _OBJECT_WORDS[key] = found
    return found


def _marked(text: str, context: EditContext | None) -> str:
    """The request with every name as one word saying what it names ("qzvisual", "qzpage",
    "qzfield"), so a name with "of" or "by" in it stays in one piece."""
    if context is None:
        return text
    spans: list[tuple[int, int, str]] = []
    for entry in context.visuals:
        if not entry.is_group and entry.caption:
            spans += [(s, e, "qzvisual") for s, e in _name_spans(text, entry.caption)]
    for page in context.pages:
        spans += [(s, e, "qzpage") for s, e in _name_spans(text, page.name)]
    for hit in match_fields(text, context):
        spans.append((hit.start, hit.end, "qzfield"))
    chosen: list[tuple[int, int, str]] = []
    for span in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if all(span[0] >= c[1] or span[1] <= c[0] for c in chosen):
            chosen.append(span)
    out = text
    for start, end, word in sorted(chosen, reverse=True):
        out = f"{out[:start]} {word} {out[end:]}"
    return out


def _removed_objects(text: str, *, off: bool = False) -> list[tuple[str, str]]:
    """What each removal word of the request removes, with the words after it: "remove the
    border from the slicer" removes "the border"; "take the data bars off the table" removes
    "the data bars". With ``off``, also what "hide", "turn off" and "no more" switch off."""
    plain = _plain(text)
    found: list[tuple[str, str]] = []
    patterns = (_REMOVAL_AT, _OFF_AT) if off else (_REMOVAL_AT,)
    for pattern in patterns:
        for match in pattern.finditer(plain):
            inside = match.groupdict().get("taken") or match.groupdict().get("turned")
            if inside is not None:
                found.append((" ".join(inside.split()), plain[match.end() :]))
                continue
            rest = re.sub(r"^\s*(?:showing|displaying|to\s+show)\b", "", plain[match.end() :])
            end = _OBJECT_END.search(rest)
            phrase = rest[: end.start()] if end else rest
            found.append((" ".join(phrase.split()[:6]), rest[end.start() :] if end else ""))
    return [(phrase, after) for phrase, after in found if phrase]


def _removes_a_field_from_a_visual(text: str, context: EditContext) -> bool:
    """Whether a removal word takes a field the request names out of a visual ("take
    Employee Name out of the table", "remove Rank from the chart", "get rid of the Rank
    column in the matrix"), not a part of it or the visual itself."""
    if re.search(r"\b(?:bookmark|group|filter|page|report|model)s?\b", text, re.I):
        return False
    marked = _marked(text, context)
    if "qzfield" not in marked:
        return False
    for phrase, after in _removed_objects(marked):
        if (
            "qzfield" in phrase
            and not _PART_NOUNS.search(phrase.replace("qzfield", ""))
            and (
                re.search(r"\b(?:qzvisual|table|matrix|chart|graph|visual|card)\b", after, re.I)
                or re.search(r"\b(?:table|matrix|chart|graph|visual|card)\b", phrase, re.I)
            )
        ):
            return True
    return False


def _removing(card: ToolCard) -> bool:
    return re.match(r"^(?:delete|clear|remove|drop)_", card.op) is not None


def _destroys(card: ToolCard) -> bool:
    """A card that deletes a whole object (a visual, a page, a measure): "hide" is no delete."""
    return card.op.startswith(("delete_", "drop_"))


def _removes_this(card: ToolCard, phrase: str, after: str) -> bool:
    """Whether what a removal word removes is what the card removes: "remove the border"
    deletes no visual, "remove the Rank column from the table" deletes no measure."""
    said = _VISUAL_COMPOUND.sub(
        lambda m: "textbox" if "text" in m.group(0).casefold() else "", phrase
    )
    if card.op == "delete_visual":
        return (
            _VISUAL_REFS.search(said) is not None
            and _PART_NOUNS.search(said) is None
            and _FIELD_NOUNS.search(said) is None
            and "qzfield" not in said
        )
    if card.op == "delete_page":
        return re.search(r"\b(?:pages?|qzpage|tabs?)\b", said, re.I) is not None
    if card.op == "delete_measure":
        from_visual = re.match(
            r"\s*(?:from|in|on|off)\s+(?:the\s+|this\s+|that\s+)?(?:qzvisual|visual|chart|graph|"
            r"table|matrix|card|slicer)\b",
            after,
            re.I,
        )
        return (
            re.search(r"\b(?:measures?|qzfield)\b", said, re.I) is not None
            and re.search(r"\bcolumns?\b", said, re.I) is None
            and from_visual is None
        )
    own = _object_words(card)
    return bool(set(tokens(said)) & own)


def _switches_off(card: ToolCard, phrase: str) -> bool:
    """Whether a card that adds or sets can also turn off what the words name: a show or
    hidden slot of its own, a bool slot the words name, or a choice that removes."""
    for slot in card.slots:
        if slot.kind == "bool" and (
            slot.name in _SHOW_SLOTS | _HIDE_SLOTS
            or slot.name.startswith(_INVERTED)
            or _slot_at(slot, phrase) is not None
        ):
            return True
        if slot.kind == "enum" and {c.casefold() for c in slot.choices} & {
            "remove",
            "none",
            "off",
            "hide",
            "clear",
            "hidden",
        }:
            return True
    return False


def _undoes(card: ToolCard, marked: str) -> bool:
    """Whether the request removes or switches off what a card that adds or sets makes, when
    the card cannot switch it off: "remove the data bars" asks for no data bars."""
    own = _object_words(card)
    for phrase, _after in _removed_objects(marked, off=True):
        if set(tokens(phrase)) & own and not _switches_off(card, phrase):
            return True
    return False


def _asked_for(card: ToolCard, text: str, context: EditContext | None = None) -> bool:
    """Whether the request asks for what the card does, as far as removing goes: a card that
    deletes or clears only when a removal word removes the card's own object ("remove the
    border from the slicer" deletes no visual); a card that adds or sets not when the request
    removes what it makes and the card cannot switch that off."""
    marked = _marked(text, context)
    if card.op == "add_page" and not re.search(
        r"\b(?:add|create|insert|new|duplicate)\b(?:\s+\w+){0,3}\s+pages?\b", marked, re.I
    ):
        return False
    if (
        card.op == "rename_column"
        and _VISUAL_SCOPE.search(marked)
        and not _MODEL_SCOPE.search(marked)
    ):
        return False
    if _removing(card):
        found = _removed_objects(marked, off=not _destroys(card))
        return any(_removes_this(card, phrase, after) for phrase, after in found)
    return not _undoes(card, marked)


def _asked_in(card: ToolCard, pieces: Sequence[str], context: EditContext) -> bool:
    """``_asked_for`` over the pieces of a request: a removal in one piece asks for a removing
    card; a card that adds or sets is refused when any piece removes what it makes."""
    if _removing(card):
        return any(_asked_for(card, piece, context) for piece in pieces)
    return any(_asked_for(card, piece, context) for piece in pieces) and all(
        not _undoes(card, _marked(piece, context)) for piece in pieces
    )


def _siblings(first: ToolCard, second: ToolCard) -> bool:
    """A card and the card that removes what it adds ("set_conditional_format" and
    "clear_conditional_format")."""
    if _removing(first) == _removing(second):
        return False
    a = set(tokens(first.op.replace("_", " "))) - _VERB_WORDS
    b = set(tokens(second.op.replace("_", " "))) - _VERB_WORDS
    return bool(a & b)


_TARGET_KINDS = frozenset({"visual", "visuals", "page"})
_SELECTORS = frozenset({"target", "axis", "name"})  # which part, not what it becomes
# The operations whose handler refuses a call that sets nothing ("Say what to change in the
# legend"). A family's "set_tooltip_page" with only its page is a whole change, so the rule is
# only for these.
SETTINGS_OPS = frozenset(
    {"set_position", "update_measure", "set_number_format", "set_table_layout"}
)


def _changes_nothing(card: ToolCard, values: Mapping[str, Any]) -> bool:
    """A settings card filled with its target and no setting: the edit layer would refuse it
    ("say what to change") after the Apply; better not to offer it."""
    if card.op not in SETTINGS_OPS:
        return False
    settings = [s for s in card.slots if s.kind not in _TARGET_KINDS and s.name not in _SELECTORS]
    if not settings or any(s.required for s in settings):
        return False
    return not any(values.get(s.name) is not None for s in settings)


def validation_problem(operation: Mapping[str, Any]) -> str | None:
    """Why ``report_edits.parse_operations`` refuses the operation, or None."""
    from . import report_edits  # the facade imports every family: loaded when first needed

    try:
        report_edits.parse_operations([dict(operation)])
    except DocumentError as error:
        return re.sub(r"^Step 1 \(\w+\):\s*", "", str(error))
    return None


def _value_label(slot: Slot, value: Any, context: EditContext) -> str:
    kind = slot.kind
    if kind == "visual" and isinstance(value, str):
        entry = context.visual(value)
        return _visual_label(entry, context) if entry else value
    if kind == "visuals" and isinstance(value, list):
        labels = [
            _visual_label(e, context) for v in value if (e := context.visual(str(v))) is not None
        ]
        return ", ".join(labels[:4]) + (f" and {len(labels) - 4} more" if len(labels) > 4 else "")
    if kind == "page" and isinstance(value, str):
        page = context.page(value)
        return _page_label(page) if page else f"page '{value}'"
    if kind in _FIELD_KINDS:
        items = value if isinstance(value, list) else [value]
        names = [
            str(item.get("name")) if isinstance(item, Mapping) else str(item) for item in items
        ]
        return ", ".join(names)
    if kind == "bool":
        return "on" if value else "off"
    if kind == "color" and isinstance(value, str):
        hue = hue_family(value)
        return f"{hue} ({value})" if hue else value
    if kind == "enum":
        return str(value).replace("_", " ")
    if kind in {"number", "integer"} and isinstance(value, int | float):
        return plain_number(float(value))
    if kind == "text":
        return f"“{value}”"
    if kind in {"dax", "m"}:
        return " ".join(str(value).split())  # the whole formula: it is what gets applied
    return str(value)


def _headline(card: ToolCard) -> str:
    """The short line a step of this card starts with (``ToolCard.label``)."""
    return card.label


_MODES = {"add": "Add", "remove": "Remove", "replace": "Replace"}


def step_title(card: ToolCard, filled: Mapping[str, Any]) -> str:
    """The line a step starts with, worded from what it does: "Remove fields" for a removal,
    "Hide a page" for a hide, "Remove the filters of a page" at page scope."""
    mode = filled.get("mode")
    if isinstance(mode, str) and mode in _MODES and card.slot("mode") is not None:
        listed = next((s for s in card.slots if s.kind in {"fields", "visuals"}), None)
        noun = listed.name.replace("_", " ") if listed is not None else "fields"
        return f"{_MODES[mode]} {noun}"
    hidden = next(
        (filled[s.name] for s in card.slots if s.name in _HIDE_SLOTS and s.name in filled), None
    )
    title = card.label
    if isinstance(hidden, bool) and title.casefold().startswith("hide or show "):
        return ("Hide " if hidden else "Show ") + title[len("hide or show ") :]
    if card.op == "clear_filter":
        scope = filled.get("scope")
        where = {"visual": "of a visual", "page": "of a page", "report": "of the report"}
        return f"Remove the filters {where.get(str(scope), '')}".strip()
    return title


def _part_label(slot: Slot, value: Any, context: EditContext) -> str | None:
    """How one filled slot reads in a step's line (None: said by the line's start)."""
    if slot.kind in {"visual", "page"}:
        return _value_label(slot, value, context)
    if slot.kind == "bool":
        if slot.name in _HIDE_SLOTS:
            return None
        if slot.name in _SHOW_SLOTS:
            return "shown" if value else "hidden"
        name = slot.name.replace("_", " ")
        if slot.name.startswith(_INVERTED):
            return name if value else f"not {name}"
        return f"{name} {_value_label(slot, value, context)}"
    if slot.name == "mode" and slot.kind == "enum":
        return None
    return f"{slot.name.replace('_', ' ')} {_value_label(slot, value, context)}"


def describe_step(card: ToolCard, filled: Mapping[str, Any], context: EditContext) -> str:
    parts = [
        said
        for slot in card.slots
        if slot.name in filled
        and filled[slot.name] is not None
        and not (card.op == "clear_filter" and slot.name == "scope")  # the line's start says it
        and (said := _part_label(slot, filled[slot.name], context)) is not None
    ]
    title = step_title(card, filled)
    return f"{title}: {', '.join(parts)}." if parts else f"{title}."


def _disagreement(filled: Mapping[str, Any], operation: Mapping[str, Any]) -> str | None:
    """Why a step whose table and column do not agree is refused: the table slot, the table of
    its column slot and the table the built operation names must be one table (a StoreKey that
    FactSales and DimStore both have once hid FactSales' while the line said DimStore)."""
    for prefix in ("", "from_", "to_"):
        column = filled.get(f"{prefix}column")
        named = {
            str(t)
            for t in (
                filled.get(f"{prefix}table"),
                column.get("table") if isinstance(column, Mapping) else None,
                operation.get(f"{prefix}table") if column is not None else None,
            )
            if isinstance(t, str) and t
        }
        if len(named) > 1:
            return "the table it names and the table of its column differ: " + ", ".join(
                sorted(named)
            )
    return None


# What a model operation sets on a column, as the line before Apply says it.
_COLUMN_SETTINGS = (
    ("format_string", "format {t} as “{v}”"),
    ("sort_by", "sort {t} by {v}"),
    ("summarize_by", "summarize {t} by {v}"),
    ("data_category", "categorize {t} as {v}"),
    ("new_name", "rename {t} to “{v}”"),
    ("description", "describe {t} as “{v}”"),
)


def operation_line(
    card: ToolCard, filled: Mapping[str, Any], operation: Mapping[str, Any], context: EditContext
) -> str:
    """The line a step shows, built from the operation that will be sent (never from the
    words): "Hide DimStore[StoreKey] in the model"; for any other card, its slots as the
    operation carries them."""
    table, name = operation.get("table"), operation.get("name") or operation.get("column")
    if card.needs_desktop and "column" in card.op and isinstance(table, str) and name:
        target = f"{table}[{name.get('name') if isinstance(name, Mapping) else name}]"
        said: list[str] = []
        hidden = operation.get("hidden")
        if hidden is not None:
            said.append(f"{'hide' if hidden else 'show'} {target} in the model")
        for key, pattern in _COLUMN_SETTINGS:
            value = operation.get(key)
            if value is not None:
                shown = value.get("name") if isinstance(value, Mapping) else value
                said.append(pattern.format(t=target if not said else "it", v=shown))
        if card.op.startswith("delete_"):
            said.insert(0, f"delete {target} from the model")
        if card.op.startswith("create_"):
            said.insert(0, f"create the column {target}")
        if said:
            line = ", and ".join(said)
            return line[0].upper() + line[1:] + "."
        return f"{card.label}: {target}."
    shown = dict(filled)
    for slot in card.slots:
        key = slot.field or slot.name
        if "." not in key and key in operation and slot.kind not in _FIELD_KINDS:
            shown[slot.name] = operation[key]  # the value sent, as sent
    if isinstance(table, str) and card.slot("table") is not None:
        shown["table"] = table
    return describe_step(card, shown, context)


def build_step(
    card: ToolCard,
    filled: Mapping[str, Any],
    context: EditContext,
    *,
    source: Literal["cards", "model"],
    notes: Sequence[str] = (),
    request: str = "",
) -> PlanStep | str:
    """The validated step for these slot values, or why it cannot be made (plain words)."""
    try:
        operation = card.to_operation(filled)
    except (KeyError, TypeError, ValueError) as error:
        return f"the values do not fit ({error})"
    made = str(operation.get("op"))
    if made != card.op:
        return "the card made another operation than its own"
    if made == "set_conditional_format" and request and operation.get("style") == "rules":
        # A strict comparison must stay strict at the threshold. Manual operations keep
        # the handler's documented inclusive defaults; this is grounded request intent.
        for rule in operation.get("rules", []):
            if "min" in rule and re.search(
                r"\b(?:above|greater\s+than|over)\s+-?\d",
                request,
                re.I,
            ):
                rule["min_inclusive"] = False
            if "max" in rule and re.search(
                r"\b(?:at\s+most|less\s+than\s+or\s+equal\s+to)\s+-?\d", request, re.I
            ):
                rule["max_inclusive"] = True
    problem = validation_problem(operation) or _disagreement(filled, operation)
    if problem is not None:
        return problem
    return PlanStep(
        op=card.op,
        operation=operation,
        summary=operation_line(card, filled, operation, context),
        needs_desktop=card.needs_desktop,
        confirm=card.confirm,
        source=source,
        notes=tuple(notes),
        slots=dict(filled),
    )


# --- checking a formula -----------------------------------------------------------------------


def check_dax(expression: str, context: EditContext) -> list[str]:
    """Problems the local DAX parser and the model's names find in a measure or column
    formula (advisory beyond that: Power BI Desktop evaluates it when it is applied)."""
    from .dax_syntax import (  # the DAX grammar is only needed when a formula is checked
        Binary,
        Call,
        ColRef,
        DaxParseError,
        Node,
        TableCtor,
        Unary,
        VarBlock,
        is_query,
        parse_expression,
    )

    text = expression.strip()
    if not text:
        return ["The formula is empty."]
    if is_query(text):
        return ["That is a query (EVALUATE); a measure needs an expression."]
    try:
        node = parse_expression(text)
    except (DaxParseError, ValueError) as error:
        return [f"It is not valid DAX: {error}."]
    tables = {t.casefold(): {c.casefold() for c in cols} for t, cols in context.tables.items()}
    measures = {m.name.casefold() for m in context.measures}
    columns = {c for cols in tables.values() for c in cols}
    problems: list[str] = []

    def walk(item: Node) -> None:
        if isinstance(item, ColRef):
            name = item.column.casefold()
            if item.table is None:
                if name not in measures and name not in columns:
                    problems.append(f"There is no measure or column [{item.column}].")
            elif item.table.casefold() in tables:
                if name not in tables[item.table.casefold()] and name not in measures:
                    problems.append(f"{item.table} has no column [{item.column}].")
            elif item.table:
                problems.append(f"There is no table '{item.table}'.")
        elif isinstance(item, Call):
            for arg in item.args:
                walk(arg)
        elif isinstance(item, Binary):
            walk(item.left)
            walk(item.right)
        elif isinstance(item, Unary):
            walk(item.operand)
        elif isinstance(item, VarBlock):
            for _, value in item.variables:
                walk(value)
            walk(item.body)
        elif isinstance(item, TableCtor):
            for row in item.rows:
                for cell in row:
                    walk(cell)

    walk(node)
    return list(dict.fromkeys(problems))


def check_m(expression: str, context: EditContext) -> list[str]:
    """Problems the Power Query validator finds (``llm/m_validation.cjs`` under Node). When
    Node is missing the check says so and the expression stays unverified."""
    from ai_powerbi_analyst.llm.artifact_checks import problems  # runs Node: only when needed

    return problems("Power Query M code", expression)


def _unverified(problem: str) -> bool:
    return "could not be verified" in problem or "unavailable" in problem


def describe_formula(expression: str, context: EditContext) -> str | None:
    from .dax_describe import describe

    return describe(expression, {m.name for m in context.measures})


# --- the prompt -----------------------------------------------------------------------------

PLAN_SYSTEM = (
    "You plan edits to a Power BI report. Reply with JSON only.\n"
    'kind: "plan" when the tools can do what the request asks; "unclear" when you cannot tell '
    'what to change; "unsupported" when no tool does it.\n'
    "steps: one step per change the request asks for, in its order, at most 4. tool is a "
    "tool number.\n"
    "slots: only what the request says. A V, P, F or T slot takes a number from that list. A "
    "choice slot takes one of its words. A color is #RRGGBB or a color word. A text slot "
    "copies words from the request exactly, including column names. Fill every setting asked "
    "for; leave out settings not asked for. 'Drop shadow' means add a shadow. Never add a "
    "change that was not asked."
)

DRAFT_SYSTEM = (
    "Write one DAX expression for a Power BI {what}. Reply with JSON only: "
    '{{"expression": "..."}}. Use only the listed names, written exactly: measures as '
    "[Name], columns as Table[Column] ('Table'[Column] when the name has spaces). No "
    "EVALUATE, no name before an equals sign, no comments."
)
DRAFT_SYSTEM_M = (
    "Write one Power Query M expression. Reply with JSON only: "
    '{"expression": "..."}. Use only the listed tables and columns. No comments.'
)


@dataclass(frozen=True)
class _Shape:
    cards: int
    examples: bool
    short: bool
    visuals: int
    fields: int
    optional: int | None = None  # optional slots shown per card (the ones the request names first)
    choices: int | None = None  # choices shown per list (idem)


_SHAPES = (
    _Shape(6, True, False, 20, 24),
    _Shape(6, False, False, 20, 24),
    _Shape(6, False, False, 12, 14),
    _Shape(6, False, True, 12, 14, 8, 12),
    _Shape(5, False, True, 12, 14, 6, 8),
    _Shape(4, False, True, 10, 10, 5, 8),
    _Shape(3, False, True, 8, 8, 4, 6),
    _Shape(3, False, True, 6, 6, 3, 5),
)


def _mentioned(name: str, words: set[str]) -> bool:
    return bool(set(tokens(name.replace("_", " "))) & words)


def _kind_text(slot: Slot, shape: _Shape | None = None, words: set[str] | None = None) -> str:
    kind = slot.kind
    if kind == "visual":
        return "V"
    if kind == "visuals":
        return "V list"
    if kind == "page":
        return "P"
    if kind == "field":
        return "F"
    if kind == "fields":
        return "F list"
    if kind == "measure":
        return "F measure"
    if kind == "column":
        return "F column"
    if kind == "table":
        return "T"
    if kind == "enum":
        limit = shape.choices if shape is not None else None
        if limit is None or len(slot.choices) <= limit:
            return "|".join(slot.choices)
        said = words or set()
        # a long list (25 icon sets): the choices the request names, then the first ones
        ranked = sorted(slot.choices, key=lambda c: not _mentioned(c, said))
        return "|".join(ranked[:limit]) + "|..."
    if kind in {"number", "integer"}:
        low = plain_number(slot.minimum) if slot.minimum is not None else ""
        high = plain_number(slot.maximum) if slot.maximum is not None else ""
        bounds = f" {low}..{high}" if low or high else ""
        return ("int" if kind == "integer" else "num") + bounds
    return kind


def _prompt_slots(card: ToolCard, text: str = "", context: EditContext | None = None) -> list[Slot]:
    """Offer requested settings, required inputs and targets; formulas are drafted separately."""
    slots = [s for s in card.slots if s.kind not in {"dax", "m"}]
    if not text:
        return slots
    marked = _marked(text, context)
    colors = find_colors(marked, context.palette if context is not None else None)
    numbers = _numbers_in(marked)
    condition = card.slot("condition")
    chosen_style = _conditional_style(card, marked)
    style_settings = {
        "rules": {"threshold", "threshold_unit", "below_color", "above_color", "blank"},
        "gradient": {
            "min_color",
            "max_color",
            "mid_color",
            "min_value",
            "max_value",
            "mid_value",
            "blank",
        },
        "data_bars": {"positive_color", "negative_color", "bar_only"},
        "icons": {"icon_set", "icon_position", "icon_alignment", "threshold"},
        "field_value": {"measure"},
    }
    conditional_settings = set().union(*style_settings.values())

    def allowed(slot: Slot) -> bool:
        if (
            card.op == "add_button"
            and slot.name in {"target_page", "target_bookmark", "url"}
            and _BACK_BUTTON.search(marked)
        ):
            return False  # a back button goes back: no destination to name
        if slot.required or slot.kind in {"visual", "visuals", "page", "table"}:
            return True
        if card.op in _RELATIONSHIP_OPS and slot.kind in {"bool", "enum"}:
            return relationship_setting(slot.name, marked) is not None
        if (
            card.op == "set_conditional_format"
            and isinstance(chosen_style, str)
            and chosen_style in style_settings
            and slot.name in conditional_settings
            and slot.name not in style_settings[chosen_style]
        ):
            return False
        if slot.kind == "bool":
            return _bool_value(slot, marked, card) is not None
        if slot.kind in {"number", "integer"}:
            return (
                bool(numbers)
                or _talked_about(slot, marked)
                or (
                    slot.name == "shade"
                    and bool(re.search(r"\b(?:lighten|darken|lighter|darker)\b", marked, re.I))
                )
            )
        if slot.kind == "color":
            return bool(colors)
        if slot.kind in _FIELD_KINDS:
            return slot.name in {"field", "fields", "date", "date_column"} or _talked_about(
                slot, marked
            )
        if slot.kind == "enum":
            if slot.name == "target" and chosen_style in {"data_bars", "icons"}:
                return False
            if slot.name == "apply_to":
                return _talked_about(slot, marked) or bool(
                    re.search(r"\b(?:cells?|totals?|subtotals?)\b", marked, re.I)
                )
            if slot.name in {"part", "target"}:
                return True  # another filled slot can name its selector (category, series)
            if card.op == "set_conditional_format" and slot.name in _ICON_PLACES:
                return _talked_about(slot, marked) or _icon_place(slot, marked) is not None
            if card.op == "set_conditional_format" and slot.name == "icon_set":
                return _talked_about(slot, marked) or bool(_icon_kinds(card, marked))
            return _talked_about(slot, marked) or any(
                _choice_backed(slot, choice, marked) is True for choice in slot.choices
            )
        if slot.kind == "text":
            if card.op == "sync_slicer" and slot.name == "hidden_on":
                return _talked_about(slot, marked) or bool(_HIDDEN_THERE.search(marked))
            if card.op == "sync_slicer" and slot.name == "group":
                return _talked_about(slot, marked)
            if slot.name in {"format", "format_string", "currency_symbol"}:
                return bool(
                    re.search(
                        r"\b(?:format(?:ting)?|currency|dollars?|euros?|pounds?|percent(?:age)?)\b|[#$€£]|0\.0",
                        marked,
                        re.I,
                    )
                )
            if slot.name in {"name", "new_name"}:
                return bool(
                    re.search(r"\b(?:call(?:ed)?|nam(?:e|ed)|rename|label)\b", marked, re.I)
                )
            if (
                slot.name == "field"
                and card.op in {"set_number_format", "set_conditional_format"}
                and context is not None
            ):
                target_type = _target_type(text, context)
                if target_type is not None and target_type not in _TABLE_TYPES:
                    return False
            if slot.name == "value" and condition is not None:
                return isinstance(_choice_in(condition, marked), str)
            if card.op == "add_button" and slot.name in {"text", "tooltip"}:
                return _talked_about(slot, marked) or bool(quoted(marked))
            return True
        return False

    return [s for s in slots if allowed(s)]


def _tool_text(
    number: int,
    card: ToolCard,
    shape: _Shape,
    words: set[str] | None = None,
    *,
    text: str = "",
    context: EditContext | None = None,
) -> str:
    said = words or set()
    slots = _prompt_slots(card, text, context)
    optional = [s for s in slots if not s.required]
    if shape.optional is not None and len(optional) > shape.optional:
        # a card with twenty settings: those the request names, then the card's first ones
        optional = sorted(optional, key=lambda s: not _mentioned(s.name, said))[: shape.optional]

    def shown(slot: Slot) -> str:
        kind = _kind_text(slot, shape, said)
        if slot.kind == "text" and slot.name == "field":
            kind += " (exact column name)"
        elif slot.kind == "text" and slot.name == "values":
            kind += " (values to keep)"
        elif slot.name == "page" and card.slot("target_page") is not None:
            kind += " (place on current page unless said)"
        return f"{slot.name}={kind}"

    need = [shown(s) for s in slots if s.required]
    may = [shown(s) for s in optional]
    summary = f"{_headline(card)}." if shape.short else card.summary
    line = f"{number} {card.op}: {summary}"
    if need:
        line += " Needs: " + ", ".join(need) + "."
    if may:
        line += " May: " + ", ".join(may) + "."
    drafted = [s for s in card.slots if s.kind in {"dax", "m"}]
    if drafted:
        line += " (Studio writes the formula.)"
    if shape.examples and card.examples:
        example = max(card.examples, key=lambda item: len(set(_content(item[0])) & said))[0]
        line += f'\n  e.g. "{m_prompt_text(example)}"'
    return line


def _short(text: str, limit: int = 34) -> str:
    """At most ``limit`` characters, cut at a word ("...above 100K" must not read "above 10")."""
    if len(text) <= limit:
        return text
    return text[: limit - 3].rsplit(" ", 1)[0].rstrip(" ,") + "..."


def _visual_row(number: int, entry: VisualEntry, page: PageEntry | None) -> str:
    shown = _short(entry.caption or ", ".join(entry.fields[:2]))
    where = describe_position(entry, page) + (" hidden" if entry.hidden else "")
    return f'{number} {type_name(entry.visual_type)} "{shown}" {where}'


@dataclass
class Prompt:
    user: str
    cards: tuple[ToolCard, ...]
    lists: _Lists
    schema: dict[str, Any]

    @property
    def size(self) -> int:
        return len(PLAN_SYSTEM.encode("utf-8")) + len(self.user.encode("utf-8"))


class TooLarge(ValueError):
    """Nothing that fits the budget would still let the model choose (too many visuals and
    none named, or a very long request)."""


_VISUAL_WORDS = frozenset(
    {"table", "chart", "card", "slicer", "matrix", "visual", "page", "report", "graph", "bar"}
)


def _rank_fields(
    text: str, context: EditContext, selected: VisualEntry | None, visuals: Sequence[VisualEntry]
) -> list[tuple[str, str, str]]:
    everything: list[tuple[str, str, str]] = [
        ("measure", m.table, m.name) for m in context.measures if not m.name.startswith("_")
    ]
    everything += [
        ("column", table, column)
        for table, columns in context.tables.items()
        if not table.casefold().startswith(("localdatetable", "dateautotemplate"))
        for column in columns
        if not column.startswith("_")
    ]
    named = {(h.table, h.name) for h in match_fields(text, context, partial=True)}
    bound = {(b[1], b[2]) for b in (selected.bindings if selected else ())}
    shown = {(b[1], b[2]) for v in visuals for b in v.bindings}
    # "in the table" names a visual, not a field
    words = set(_content(text)) - _VISUAL_WORDS
    # "only show 2019" names a year: the year and date columns are what it filters
    years = re.search(r"\b(?:19|20)\d\d\b", text) is not None

    def rank(item: tuple[int, tuple[str, str, str]]) -> tuple[int, float, int]:
        position, (kind, table, name) = item
        key = (table, name)
        # "employees" names the DimEmployee table's columns as much as their own names do
        overlap = len(words & set(tokens(name)))
        if kind == "column":
            overlap += len(words & set(tokens(table)) - {"dim", "fact"})
            if years and ("year" in name.casefold() or "date" in name.casefold()):
                overlap += 1
        tiers = (key in named, key in bound, overlap > 0, key in shown, True)
        return (tiers.index(True), -overlap, position)

    return [f for _, f in sorted(enumerate(everything), key=rank)]


def _relationship_context(text: str, context: EditContext, limit: int) -> list[str]:
    """A bounded metadata-only view of the exact head; never partition queries or row values."""
    named = {table for table in context.tables if _name_spans(text, table)}
    relationships = sorted(
        context.relationships,
        key=lambda r: not bool({r.from_table, r.to_table} & named),
    )
    shown = relationships[:limit]
    tables = list(
        dict.fromkeys([*named, *(table for r in shown for table in (r.from_table, r.to_table))])
    )[: max(limit * 2, 2)]
    lines: list[str] = []
    if context.model_revision:
        lines.append("Model revision: " + m_prompt_text(context.model_revision))
    if tables:
        lines.append(
            "Storage: "
            + "; ".join(
                f"{m_prompt_text(t)}={context.storage_modes.get(t, 'unknown')}" for t in tables
            )
        )
    labels = []
    for r in shown:
        cardinality = ("many" if r.from_many else "one") + "-to-" + ("many" if r.to_many else "one")
        if not r.cardinality_known:
            cardinality = "cardinality unknown"
        security = "unknown" if r.security_both is None else "both" if r.security_both else "single"
        labels.append(
            f"{m_prompt_text(r.from_table)}[{m_prompt_text(r.from_column)}] -> "
            f"{m_prompt_text(r.to_table)}[{m_prompt_text(r.to_column)}]: {cardinality}, "
            f"{'active' if r.active else 'inactive'}, filter={'both' if r.both else 'single'}, "
            f"security={security}, "
            f"assume-RI={'unknown' if r.rely is None else str(r.rely).lower()}"
        )
    status = "known" if context.relationships_known else "partial/unknown"
    extra = (
        f"; {len(relationships) - len(shown)} more omitted"
        if len(relationships) > len(shown)
        else ""
    )
    lines.append(f"Relationships ({status}{extra}): " + ("; ".join(labels) or "none listed"))
    lines.append(
        "For one-to-many, single filters one to many. Source groups/key integrity are not "
        "verified; enabling assume-RI requires Power BI."
    )
    return lines


def build_prompt(
    text: str,
    context: EditContext,
    cards: Sequence[ToolCard],
    *,
    budget: int = PROMPT_BUDGET,
) -> Prompt:
    """The user prompt, the numbered lists and the JSON schema for these cards, in the largest
    shape that fits ``budget`` bytes together with ``PLAN_SYSTEM``."""
    request = " ".join(clean(m_prompt_text(text)).split())
    if len(request) > MAX_REQUEST:
        raise TooLarge("That request is too long for me to read. Say it in one short sentence.")
    source_text = _sync_source_text(text)
    page = scope_page(context, page_words(source_text, context))
    ordered = page_visuals(context, page) if page is not None else []
    words = set(tokens(text))
    selected = context.visual(context.selection) if context.selection else None
    if selected is not None and page is not None and selected.page_id != page.id:
        selected = None
    ranked = sorted(
        range(len(ordered)),
        key=lambda i: (
            0 if selected is not None and ordered[i].id == selected.id else 1,
            -visual_relevance(ordered[i], words),
            i,
        ),
    )
    fields_ranked = _rank_fields(text, context, selected, ordered)
    for shape in _SHAPES:
        chosen = tuple(cards[: shape.cards])
        kinds = {s.kind for c in chosen for s in _prompt_slots(c, text, context)}
        lists = _Lists(page=page, on_page=ordered, pages=list(context.pages[:12]))
        lines = [f"Request: {request}"]
        if any(s.kind == "text" and s.name == "field" for c in chosen for s in c.slots):
            names = list(dict.fromkeys(h.name for h in match_fields(text, context)))
            if names:
                lines.append("Copy column text exactly: " + "; ".join(f'"{n}"' for n in names[:4]))
        if kinds & {"visual", "visuals"}:
            # the listed visuals keep the page's order, numbered from 1 with no gaps
            lists.visuals = [ordered[i] for i in sorted(ranked[: shape.visuals])]
            lists.omitted = len(ordered) - len(lists.visuals)
            sources = _sync_visuals(text, context)
            if len(sources) == 1 and (source_number := lists.number(sources[0])) is not None:
                lines.append(f'Source V{source_number}: "{sources[0].caption}"')
            for entry in named_visuals(source_text, context):
                number = lists.number(entry)
                fields = [name for name in entry.fields if _name_spans(text, name)]
                fields = [n for n in fields if not any(n != m and n in m for m in fields)]
                if number is not None and fields:
                    lines.append(
                        f"Target V{number} {type_name(entry.visual_type)} fields: "
                        + "; ".join(f'"{name}"' for name in fields[:3])
                    )
            if selected is not None and (number := lists.number(selected)) is not None:
                lines.append(f"Selected: V{number}")
        lines.append("Tools:")
        lines += [
            _tool_text(n, card, shape, words, text=text, context=context)
            for n, card in enumerate(chosen, 1)
        ]
        if kinds & {"visual", "visuals"}:
            title = f'V (page "{page.name}")' if page is not None else "V"
            rows = [_visual_row(n, entry, page) for n, entry in enumerate(lists.visuals, 1)]
            more = f" (+{lists.omitted} more not listed)" if lists.omitted else ""
            lines.append(f"{title}{more}: " + "; ".join(rows))
        if "page" in kinds:
            lines.append("P: " + "; ".join(f'{n} "{p.name}"' for n, p in enumerate(lists.pages, 1)))
        if kinds & _FIELD_KINDS:
            lists.fields = fields_ranked[: shape.fields]
            lines.append(
                "F: " + "; ".join(f"{n} {_field_label(*f)}" for n, f in enumerate(lists.fields, 1))
            )
        if "table" in kinds:
            lists.tables = list(context.tables)[: shape.fields]
            lines.append("T: " + "; ".join(f"{n} {t}" for n, t in enumerate(lists.tables, 1)))
        if any(card.op in _RELATIONSHIP_OPS for card in chosen):
            lines += _relationship_context(text, context, min(4, max(1, shape.fields // 8)))
        user = "\n".join(lines)
        size = len(PLAN_SYSTEM.encode("utf-8")) + len(user.encode("utf-8"))
        if size <= budget:
            if (
                all(any(s.kind in {"visual", "visuals"} for s in c.slots) for c in chosen)
                and lists.omitted
                and selected is None
                and not any(visual_relevance(entry, words) > 0 for entry in lists.visuals)
            ):
                # every tool needs a visual and the request names none of those that fit

                name = page.name if page is not None else ""
                raise TooLarge(
                    f"Page '{name}' has {len(ordered)} visuals, too many for me to read at once. "
                    "Select the visual on the canvas, or name it by its title."
                )
            return Prompt(user, chosen, lists, plan_schema(chosen, lists, text, context))
    raise TooLarge("That request is too long for me to read. Say it in one short sentence.")


def _slot_schema(
    slot: Slot,
    lists: _Lists,
    text: str = "",
    context: EditContext | None = None,
    card: ToolCard | None = None,
) -> dict[str, Any] | None:
    kind = slot.kind
    if kind in {"visual", "visuals"}:
        if not lists.visuals:
            return None
        wanted = set()
        if text and context:
            source_text = _sync_source_text(text)
            wanted = {
                v.id
                for v in named_visuals(source_text, context)
                if v.caption and _name_spans(source_text, v.caption)
            }
            resolution = resolve_visuals(source_text, context)
            if resolution.ok and resolution.explicit and resolution.how != "selection":
                wanted |= {v.id for v in resolution.entries}
            sources = _sync_visuals(text, context)
            if len(sources) == 1:
                wanted = {sources[0].id}
        if not wanted and context is not None and context.selection:
            wanted = {context.selection}
        numbers = [n for n, v in enumerate(lists.visuals, 1) if not wanted or v.id in wanted]
        one: dict[str, Any] = {"enum": numbers or list(range(1, len(lists.visuals) + 1))}
        return one if kind == "visual" else {"type": "array", "items": one, "maxItems": 12}
    if kind == "page":
        return {"enum": list(range(1, len(lists.pages) + 1))} if lists.pages else None
    if kind in _FIELD_KINDS:
        field_kind = {"measure": "measure", "column": "column"}.get(kind)
        numbers = [
            n for n, f in enumerate(lists.fields, 1) if field_kind is None or f[0] == field_kind
        ]
        if not numbers:
            return None
        item: dict[str, Any] = {"enum": numbers}
        return item if kind != "fields" else {"type": "array", "items": item, "maxItems": 12}
    if kind == "table":
        return {"enum": list(range(1, len(lists.tables) + 1))} if lists.tables else None
    if kind == "enum":
        if card is not None and card.op in _RELATIONSHIP_OPS:
            stated = relationship_setting(slot.name, _marked(text, context))
            return {"enum": [stated]} if isinstance(stated, str) else {"enum": list(slot.choices)}
        if slot.name == "kind" and card is not None and card.op == "add_navigator":
            navigator = re.search(r"\b(bookmark|page)\s+navigator\b", text, re.I)
            if navigator:
                return {"enum": [navigator.group(1).lower()]}
        marked_text = _marked(text, context) if text else ""
        if card is not None and card.op == "set_conditional_format" and text:
            if slot.name == "style" and isinstance(
                style := _conditional_style(card, marked_text), str
            ):
                return {"enum": [style]}
            if slot.name == "icon_set" and (kinds := _icon_kinds(card, marked_text)):
                return {"enum": kinds}
            place = _icon_place(slot, marked_text) if slot.name in _ICON_PLACES else None
            if isinstance(place, str):
                return {"enum": [place]}
        backed = [c for c in slot.choices if text and _choice_backed(slot, c, marked_text) is True]
        if backed and card is not None:
            offered = {s.name for s in _prompt_slots(card, text, context)}
            backed += [c for c in slot.choices if c not in backed and c in offered]
        return {"enum": backed or list(slot.choices)} if slot.choices else None
    if kind == "bool":
        if card is not None and card.op in _RELATIONSHIP_OPS:
            stated = relationship_setting(slot.name, _marked(text, context))
            if isinstance(stated, bool):
                return {"const": stated}
        return {"type": "boolean"}
    if kind in {"number", "integer"}:
        schema: dict[str, Any] = {"type": "integer" if kind == "integer" else "number"}
        if slot.minimum is not None:
            schema["minimum"] = slot.minimum
        if slot.maximum is not None:
            schema["maximum"] = slot.maximum
        return schema
    if kind == "color":
        return {"type": "string", "maxLength": 24}
    if kind == "text":
        if card is not None and card.op == "sync_slicer" and slot.name == "pages" and context:
            pages = [p.name for p in find_pages_by_name(text, context)]
            if pages:
                return {"enum": list(dict.fromkeys(pages + [", ".join(pages)]))}
        if card is not None and card.op == "create_measure" and slot.name == "name":
            definition = new_measure_calculation(text)
            if definition is not None:
                return {"enum": [definition[0]]}
        if text and context is not None and slot.name == "field":
            named = list(dict.fromkeys(h.name for h in match_fields(text, context)))
            if not named:
                named = list(
                    dict.fromkeys(
                        name
                        for v in context.visuals
                        for name in v.fields
                        if len(name) >= 3 and _name_spans(text, name)
                    )
                )
                named = [n for n in named if not any(n != m and n in m for m in named)]
            if named:
                return {"enum": named}
        return {"type": "string", "minLength": 1, "maxLength": 120}
    return None


def plan_schema(
    cards: Sequence[ToolCard], lists: _Lists, text: str = "", context: EditContext | None = None
) -> dict[str, Any]:
    """The JSON schema of one call: a step is one of the cards, each slot limited to what that
    call's lists hold."""
    choices: list[dict[str, Any]] = []
    for number, card in enumerate(cards, 1):
        properties = {
            slot.name: schema
            for slot in _prompt_slots(card, text, context)
            if (schema := _slot_schema(slot, lists, text, context, card)) is not None
        }
        choices.append(
            {
                "type": "object",
                "properties": {
                    "tool": {"const": number},
                    "slots": {
                        "type": "object",
                        "properties": properties,
                        "additionalProperties": False,
                    },
                },
                "required": ["tool", "slots"],
                "additionalProperties": False,
            }
        )
    return {
        "type": "object",
        "properties": {
            "kind": {"enum": ["plan", "unclear", "unsupported"]},
            "steps": {"type": "array", "items": {"anyOf": choices}, "maxItems": MAX_STEPS},
        },
        "required": ["kind", "steps"],
        "additionalProperties": False,
    }


# --- reading the model's answer -----------------------------------------------------------


def _said(value: str, text: str) -> bool:
    """Whether a text slot's words are the request's own (the model may copy, never invent):
    each item of a list, and a number as any amount the request gives (100K is 100000). A value
    with symbols ("€", "0.0%", "#,0") is compared as typed: the word comparison drops them."""

    def flat(item: str) -> str:
        return " " + " ".join(re.findall(r"[\w%#&+/-]+", item.casefold())) + " "

    said = flat(text)
    raw = " ".join(text.casefold().split())
    amounts = {a.value for a in find_amounts(text)} | {scaled(a) for a in find_amounts(text)}
    parts = [p for p in re.split(r"\s*[,;]\s*", value) if p.strip()]
    if re.search(r"[^\w\s]", value) and " ".join(value.casefold().split()) in raw:
        return True
    for part in parts or [value]:
        wanted = flat(part)
        if re.search(r"[^\w\s,;]", part):
            if " ".join(part.casefold().split()) in raw:
                continue
            return False  # a symbol the request does not have ("$" for "€")
        if wanted.strip() and wanted in said:
            continue
        number = re.fullmatch(r"\s*-?\d+(?:\.\d+)?\s*", part)
        if number and float(part) in amounts:
            continue
        return False
    return True


def _color_value(value: Any, context: EditContext) -> str | None:
    if not isinstance(value, str):
        return None
    found = find_colors(value, context.palette)
    if len(found) == 1:
        return found[0].hex
    if len(found) == 2 and sum(not color.name for color in found) == 1:
        # Models sometimes label a CSS literal, e.g. "green (#00FF00)". Accept the
        # literal only when its label agrees; request grounding below chooses the shade.
        literal = next(color for color in found if not color.name)
        if hue_family(literal.hex) is not None and all(
            hue_family(color.hex) == hue_family(literal.hex) for color in found
        ):
            return literal.hex
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", value.strip()):
        return "#" + value.strip().lstrip("#").upper()
    return None


def _requested_color_word(
    card: ToolCard,
    slot: Slot,
    value: Any,
    text: str,
    context: EditContext,
) -> Any:
    """Keep a conditional color word's canonical shade when the model gives its CSS hex.

    The slot's own word evidence fixes the endpoint first. Only an equivalent hue can
    stand for that word; explicit hex literals and different/reversed hues stay exact.
    """
    if card.op != "set_conditional_format" or slot.kind != "color" or not isinstance(value, str):
        return value
    resolution = resolve_visuals(page_words(text, context), context)
    stated = _word_value(card, slot, text, context, resolution, {})
    if not isinstance(stated, str):
        return value
    colors = find_colors(mask_names(text, context), context.palette)
    matching = [color for color in colors if color.hex.upper() == stated.upper()]
    # A gradient's endpoints can share a hue or even a hex: retain their own word/literal
    # provenance instead of letting a word at one end relax a literal at the other.
    endpoint = _conditional_endpoint_color(slot, mask_names(text, context), colors)
    if isinstance(endpoint, ColorMatch) and endpoint.hex.upper() == stated.upper():
        matching = [endpoint]
    if len(matching) == 1 and matching[0].name:
        asked_hue = hue_family(matching[0].hex)
        if asked_hue is not None and hue_family(value) == asked_hue:
            return matching[0].hex
    return value


_RULE_COMPARISON = re.compile(
    r"\b(?:below|above|under|over|less\s+than|greater\s+than|at\s+most|at\s+least)\s+-?\d"
    r"|\bfrom\s+-?\d+(?:\.\d+)?\s+up\b",
    re.I,
)


def _conditional_rule_color(
    slot: Slot,
    text: str,
    colors: Sequence[ColorMatch],
) -> ColorMatch | object | None:
    """Bind a rule color to the comparison in its phrase, without adding its opposite."""
    own = "below" if slot.name == "below_color" else "above"
    spans = [0, *(m.end() for m in re.finditer(r"[,;]|\band\b", text, re.I)), len(text)]
    found: list[ColorMatch] = []
    for start, end in zip(spans, spans[1:], strict=False):
        phrase = text[start:end]
        sides = [
            side
            for side, pattern in (
                ("below", r"\b(?:below|under|less\s+than|at\s+most)\s+-?\d"),
                (
                    "above",
                    r"\b(?:above|over|greater\s+than|at\s+least)\s+-?\d"
                    r"|\bfrom\s+-?\d+(?:\.\d+)?\s+up\b",
                ),
            )
            if re.search(pattern, phrase, re.I)
        ]
        matches = [c for c in colors if start <= c.start and c.end <= end]
        if own in sides:
            if len(sides) != 1 or len(matches) != 1:
                return _AMBIGUOUS
            found += matches
    return found[0] if len(found) == 1 else _AMBIGUOUS if found else None


def _conditional_endpoint_color(
    slot: Slot,
    text: str,
    colors: Sequence[ColorMatch],
) -> ColorMatch | object | None:
    """Bind each scale endpoint to its own color word or literal, regardless of order.

    A from/to scale has ordered endpoints. Otherwise, separate low/high phrases must
    each contain one color and one endpoint. Ambiguous phrases never back a model swap.
    """
    own = (
        "low"
        if re.match(r"(?:min|from|start|low|lowest|first)", slot.name)
        else "high"
        if re.match(r"(?:max|to|end|high|highest|last)", slot.name)
        else None
    )
    if own is None or len(colors) < 2:
        return None
    if (
        len(colors) == 2
        and re.fullmatch(r"\s*to\s*", text[colors[0].end : colors[1].start])
        and (
            re.search(r"\bfrom\s*$", text[: colors[0].start])
            or re.search(r"\bgradient\b", text, re.I)
        )
    ):
        return colors[0 if own == "low" else 1]
    spans = [0, *(m.end() for m in re.finditer(r"[,;]|\band\b", text, re.I)), len(text)]
    bound: dict[str, list[ColorMatch]] = {"low": [], "high": []}
    for start, end in zip(spans, spans[1:], strict=False):
        matches = [c for c in colors if start <= c.start and c.end <= end]
        if len(matches) != 1:
            continue
        phrase = text[start:end]
        endpoints = [
            key
            for key, pattern in (
                ("low", r"\b(?:min(?:imum)?|low(?:est)?|bottom|start|first)\b"),
                ("high", r"\b(?:max(?:imum)?|high(?:est)?|top|end|last)\b"),
            )
            if re.search(pattern, phrase, re.I)
        ]
        if len(endpoints) == 1:
            bound[endpoints[0]].append(matches[0])
    return bound[own][0] if len(bound[own]) == 1 else _AMBIGUOUS


_NOT_THIS = re.compile(
    r"\b(?:but|except|not|other\s+than|instead\s+of|rather\s+than|never|without)\s+"
    r"(?:(?:the|a|an)\s+)?(?:words?\s+|name\s+|title\s+)?[\"'“]?$",
    re.I,
)


def _negated_value(value: str, text: str) -> bool:
    """Whether the request says the text is what it does NOT want: "call the chart anything
    but Sales" gives no title "Sales"."""
    low = text.casefold()
    at = low.rfind(value.strip().casefold())  # the new text comes last ("... but Sales")
    return at >= 0 and _NOT_THIS.search(text[:at]) is not None


_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "twelve": 12, "twenty": 20, "first": 1, "second": 2, "third": 3,
    "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "once": 1, "twice": 2, "half": 0.5,
}  # fmt: skip


def _numbers_in(text: str) -> frozenset[float]:
    """Every number the words give, as said and as meant: 120K is 120000, 50% is 50 and
    0.5, "second" is 2."""
    found: set[float] = set()
    for amount in find_amounts(text):
        # "shrink the font by 2" is a change of -2: a number counts with either sign
        found |= {amount.value, scaled(amount), abs(amount.value), -abs(amount.value)}
        if amount.unit == "%":
            found.add(amount.value / 100)
    for word in re.findall(r"[a-z]+", text.casefold()):
        if word in _WORD_NUMBERS:
            found.add(float(_WORD_NUMBERS[word]))
    return frozenset(found)


@dataclass(frozen=True)
class Evidence:
    """What the request itself (and the selection) says, worked out once per plan: a value the
    model gives must be one of these, or the step asks, leaves it out or is refused."""

    text: str
    visuals: frozenset[str] = frozenset()  # the visuals the request names
    candidates: tuple[str, ...] = ()  # the visuals a name that fits several could be
    selection: str | None = None  # the selected visual, when a piece names none
    pages: frozenset[str] = frozenset()  # the pages the request names
    default_page: str | None = None  # the page on screen
    numbers: frozenset[float] = frozenset()
    colors: frozenset[str] = frozenset()
    exact: tuple[tuple[str, str], ...] = ()  # (table, name) of the fields named in full
    partial: tuple[tuple[str, str], ...] = ()  # and of those some of whose words it says
    tables: frozenset[str] = frozenset()


_CHUNK_BREAK = re.compile(
    r"\b(?:from|to|onto|into|and|than|with|like|as|by|next\s+to|below|above|under|over)\b", re.I
)


# The kinds of visual a request can say ("the chart", "that table").
_VISUAL_KIND_WORDS = {
    "chart": r"charts?|graphs?|plots?",
    "table": r"tables?|grids?",
    "matrix": r"matri(?:x|ces)|pivots?",
    "card": r"cards?|kpis?|tiles?",
    "slicer": r"slicers?|filters?\s+boxe?s?",
}


def _said_visual_kinds(text: str) -> set[str]:
    lead = r"\b(?:the|this|that|a|an|each|every|my|our)\s+(?:\w+\s+){0,2}?"
    return {
        kind
        for kind, pattern in _VISUAL_KIND_WORDS.items()
        if re.search(rf"{lead}(?:{pattern})\b", text, re.I)
    }


def _is_kind(entry: VisualEntry, kinds: set[str]) -> bool:
    kind = entry.visual_type.casefold()
    families = {
        "table": kind in {"tableex", "table"},
        "matrix": kind in {"pivottable", "matrix"},
        "card": "card" in kind or kind in {"kpi", "gauge"},
        "slicer": "slicer" in kind,
    }
    families["chart"] = not any(families.values()) and kind not in {
        "textbox",
        "image",
        "shape",
        "actionbutton",
        "basicshape",
    }
    return any(families.get(k, False) for k in kinds)


def named_visuals(text: str, context: EditContext) -> list[VisualEntry]:
    """Every visual the request names, in its order: the whole request resolved, and each part
    of it between "from", "to", "and"... ("stop the StoreName slicer from filtering the table"
    names the slicer and the table), and every visual named by its whole caption."""
    plain = page_words(text, context)
    found: list[VisualEntry] = []
    parts = [plain, *(p for p in _CHUNK_BREAK.split(plain) if p.strip())]
    kinds = _said_visual_kinds(plain)
    for part in parts:
        resolution = resolve_visuals(part, context)
        if (
            kinds
            and resolution.how == "all"
            and resolution.type_noun == "visual"
            and not find_types(part)
            and not re.search(r"\beverything\b", part, re.I)
        ):
            # A quantifier of a setting is not a quantifier of its target. Splitting
            # "remove every filter from the table" must not turn "every filter" into
            # evidence for every visual; the other part still names the table.
            continue
        if resolution.ok and resolution.explicit and resolution.how != "selection":
            found += [
                e
                for e in resolution.entries
                if e not in found
                # a field a visual shows names it only when the words do not say it is
                # another kind of visual ("a running total of MTD Sales to the chart" does
                # not name the table that shows MTD Sales)
                and (resolution.how != "field" or not kinds or _is_kind(e, kinds))
            ]
    for entry in context.visuals:
        if (
            not entry.is_group
            and len(entry.caption) >= 3
            and entry not in found
            and _name_spans(text, entry.caption)
        ):
            found.append(entry)
    return sorted(
        found,
        key=lambda entry: min(
            (start for start, _ in _name_spans(text, entry.caption)), default=len(text)
        ),
    )


def gather_evidence(pieces: Sequence[str], context: EditContext) -> Evidence:
    whole = " ".join(pieces)
    named: set[str] = set()
    candidates: list[str] = []
    unnamed = False
    for piece in pieces:
        resolution = resolve_visuals(page_words(piece, context), context)
        if resolution.ok and resolution.explicit and resolution.how != "selection":
            named |= {e.id for e in resolution.entries}
        else:
            unnamed = True
            candidates += [e.id for e in resolution.candidates]
    selection = context.selection if unnamed and context.selection else None
    if selection is not None and context.visual(selection) is None:
        selection = None
    # a visual named by its whole caption is named, whatever else the words say about it
    # ("show Avg MTD % change by Agents as a line chart": no line chart is named), and so is
    # each one a part of the request names ("from filtering the table")
    for piece in pieces:
        named |= {e.id for e in named_visuals(piece, context)}
    plain = page_words(whole, context)
    pages = {p.id for p in find_pages_by_name(plain, context)}
    page = named_page(whole, context)
    if page is not None:
        pages.add(page.id)
    default = _default_page(context)
    masked = mask_names(whole, context)  # "Top 10 Agents", "Red Sox Sales" give no values
    return Evidence(
        text=whole,
        visuals=frozenset(named),
        candidates=tuple(dict.fromkeys(candidates)),
        selection=selection,
        pages=frozenset(pages),
        default_page=default.id if default is not None else None,
        numbers=_numbers_in(masked),
        colors=frozenset(c.hex.upper() for c in find_colors(masked, context.palette)),
        exact=tuple((h.table, h.name) for h in match_fields(whole, context)),
        partial=tuple((h.table, h.name) for h in match_fields(whole, context, partial=True)),
        tables=frozenset(t for t in context.tables if _name_spans(whole, t)),
    )


@dataclass
class _Read:
    """One step of the model's answer, read: the card, the values the request backs, what is
    missing (taken from the words or the selection, else asked), the values it left out
    (``dropped``: the request does not give them) and what makes the step wrong
    (``problems``: a value that contradicts the request sinks the step)."""

    card: ToolCard
    values: dict[str, Any] = field(default_factory=dict)
    missing: list[Slot] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    retarget: list[Slot] = field(default_factory=list)  # optional targets to take from words


def read_step(
    raw: PlanStepAnswer,
    prompt: Prompt,
    text: str,
    context: EditContext,
    evidence: Evidence | None = None,
) -> _Read | str:
    """The model's step with every slot turned into the value the card takes and checked
    against the request, or why not."""
    if not 1 <= raw.tool <= len(prompt.cards):
        return "a tool that was not offered"
    card = prompt.cards[raw.tool - 1]
    ev = evidence or gather_evidence([text], context)
    lists = prompt.lists
    read = _Read(card)
    known = {s.name: s for s in card.slots}
    offered = prompt.schema["properties"]["steps"]["items"]["anyOf"][raw.tool - 1]["properties"][
        "slots"
    ]["properties"]
    converted: dict[str, Any] = {}
    for name, value in raw.slots.items():
        slot = known.get(name)
        if slot is None or value is None or slot.kind in {"dax", "m"}:
            continue
        if name not in offered:
            read.dropped.append(f"I left out the {name.replace('_', ' ')}: it was not requested.")
            continue
        made = _convert(slot, value, card, lists, context, text)
        if made is None and card.op in _SET_FILTER_OPS and name == "values":
            # categories the request does not say are left out, and the values are asked
            read.dropped.append("I left out the filter values: your request does not say them.")
            continue
        if made is None and card.op == "sync_slicer" and name in {"pages", "hidden_on"}:
            # An invented destination supplies no page. Recover the destination from
            # the request, or ask for one through _sync_pages, including single-page
            # reports where no destination exists yet.
            read.dropped.append(
                f"I left out the {name.replace('_', ' ')}: your request does not give it."
            )
            continue
        if made is None:
            said = name.replace("_", " ")
            read.problems.append(
                f"the {said} it gave is not in your request"
                if slot.kind == "text"
                else f"the {said} it gave is not one it could choose"
            )
            continue
        converted[name] = _requested_color_word(card, slot, made, text, context)
    used: set[str] = set()
    for slot in card.slots:  # in the card's order: a visual fills the first slot it is in
        if slot.name not in converted:
            continue
        value = converted[slot.name]
        verdict = _judge(slot, value, card, ev, converted, used, context)
        said = slot.name.replace("_", " ")
        if verdict == "ok":
            read.values[slot.name] = value
            if slot.kind == "visual":
                used.add(str(value))
            elif slot.kind == "visuals":
                used |= {str(v) for v in value}
        elif verdict == "sink":
            shown = _value_label(slot, value, context)
            read.problems.append(f"the {said} it gave ({shown}) is not what your request says")
        elif verdict == "drop" and not slot.required:
            read.dropped.append(
                f"I left out the {said} ({_value_label(slot, value, context)}): your request "
                "does not give it."
            )
        elif verdict == "missing" and not slot.required:
            # an optional target the request does not back: taken from the words, or the
            # step is refused (left out, "the visuals that react" would become all of them)
            read.retarget.append(slot)
    for slot in card.slots:
        if slot.name not in read.values and slot.required:
            read.missing.append(slot)
    return read


def _judge(
    slot: Slot,
    value: Any,
    card: ToolCard,
    ev: Evidence,
    converted: Mapping[str, Any],
    used: set[str],
    context: EditContext,
) -> Literal["ok", "drop", "sink", "missing"]:
    """Whether the request backs a value the model gave: ``ok``; ``missing`` (a target the
    words or the selection give otherwise, or a question does); ``drop`` (the request does
    not give it: left out, and said); ``sink`` (the request says something else)."""
    kind = slot.kind
    if kind == "visual":
        if value in used:
            return "missing"  # a visual named once fills one slot only
        if value in ev.visuals or (not ev.visuals and value == ev.selection):
            return "ok"
        return "missing"
    if kind == "visuals":
        items = [str(v) for v in value]
        allowed = set(ev.visuals) or ({ev.selection} if ev.selection else set())
        if items and all(v in allowed and v not in used for v in items):
            if ev.visuals or slot.required or _SELECTION_REF.search(page_words(ev.text, context)):
                return "ok"
            # an optional list backed only by the selection, which the words never point at
            # ("add a bookmark called Q1" hides nothing, whatever is selected)
            return "drop" if _talked_about(slot, ev.text) is False else "missing"
        return "missing"
    if kind == "page":
        if (
            _placement(card)
            and card.slot("target_page") is not None
            and slot.name in {"page", "target_page"}
        ):
            # where a button goes and where it points, each from its own words
            placed, targets, either = _button_pages(ev.text, context)
            own = placed if slot.name == "page" else targets
            if value in {p.id for p in own}:
                return "ok"
            other = converted.get("target_page" if slot.name == "page" else "page")
            if value in {p.id for p in either} and other != value:
                return "ok"  # "a button to Forecast": on it or to it, not both
            if slot.name == "page":
                page = _placed_page(card, ev.text, context)
                return "ok" if page is not None and value == page.id else "missing"
            return "missing"
        if value in ev.pages:
            return "ok"
        if not ev.pages and value == ev.default_page and _page_by_default(card, ev.text):
            return "ok"
        return "missing"
    if kind in _FIELD_KINDS:
        items = value if isinstance(value, list) else [value]
        pairs = [
            (str(i.get("table", "")), str(i.get("name")))
            if isinstance(i, Mapping)
            else ("", str(i))
            for i in items
        ]

        def backed(pair: tuple[str, str], pool: Sequence[tuple[str, str]]) -> bool:
            return any(
                name.casefold() == pair[1].casefold() and (not pair[0] or table == pair[0])
                for table, name in pool
            )

        bound: set[tuple[str, str]] = set()  # columns of a table the request names
        # a column given by its name alone belongs to the step's table slot ("from_table" for
        # "from_column")
        prefix = slot.name[: -len("column")] if slot.name.endswith("column") else ""
        partner = converted.get(f"{prefix}table")
        pairs = [(t or (str(partner) if isinstance(partner, str) else ""), n) for t, n in pairs]
        for table, name in pairs:
            if len(tables_with_column(name, context)) > 1 and context.measure(name) is None:
                if _name_spans(ev.text, name):
                    bound.add((table, name))  # the column said, of a table said
                # a column several tables have: only a table the words name will do
                named = said_tables(name, ev.text, context)
                if not named or (table and table not in named) or (not table and len(named) > 1):
                    return "missing" if slot.required else "drop"
        pool = ev.exact if ev.exact else ev.partial
        # "exclude 2018": a year the request gives names the year (or date) column
        year = re.search(r"\b(?:19|20)\d\d\b", ev.text) is not None
        if all(
            p in bound or backed(p, pool) or (year and re.search("year|date", p[1], re.I))
            for p in pairs
        ):
            return "ok"
        return "missing" if slot.required else "drop"
    if kind == "table":
        if value in ev.tables or value in _tables_of_columns(card, converted, context):
            return "ok"  # named, or the table of the column the step names
        if not ev.tables and card.op.endswith("_measure") and value == _home_table(context):
            return "ok"
        if card.op in _ROLE_OPS and any(
            any(_name_spans(ev.text, form) for form in _label_forms(column))
            for column in context.tables.get(str(value), [])
        ):
            return "ok"  # "only sees the Online channel": a table of a channel column
        return "missing" if slot.required else "drop"
    if kind == "enum":
        verdict = _choice_backed(slot, str(value), ev.text)
        if card.op in _RELATIONSHIP_OPS:
            relationship_said = relationship_setting(slot.name, _marked(ev.text, context))
            verdict = relationship_said == value if relationship_said is not None else None
        if card.op == "set_conditional_format" and slot.name in _ICON_PLACES | {"style"}:
            # the style and the icons' place as the words give them (a condition's "below 0"
            # places no icon; a named icon set is the icons style)
            said = (
                _conditional_style(card, mask_names(ev.text, context))
                if slot.name == "style"
                else _icon_place(slot, mask_names(ev.text, context))
            )
            verdict = True if said == value else False if isinstance(said, str) else None
        elif card.op == "set_conditional_format" and slot.name == "icon_set":
            kinds = _icon_kinds(card, mask_names(ev.text, context))
            verdict = (value in kinds) if kinds else None
        names_a_slot = card.slot(str(value).casefold()) is not None and (
            converted.get(str(value).casefold()) is not None
        )
        if verdict is True or names_a_slot or _implied(card, slot, converted, context) == value:
            # said; or it names another slot the step fills (part: category); or the target
            # gives it (the well a measure goes to)
            return "ok"
        if verdict is False:
            return "sink"
        return "missing" if slot.required else "drop"
    if kind == "bool":
        # (names as one word each: "show the Total MTD Sales chart data labels" has "show"
        # four words from "labels", not seven)
        said = _bool_value(slot, _marked(ev.text, context), card)
        if said is value:
            return "ok"
        if said is None:
            return "missing" if slot.required else "drop"
        return "sink"
    if kind in {"number", "integer"}:
        rivals = [s for s in card.slots if s.kind in {"number", "integer"}]
        marked = _marked(ev.text, context)
        named_settings = [s for s in rivals if _talked_about(s, marked)]
        if not slot.required and named_settings and slot not in named_settings:
            return "drop"
        if any(math.isclose(float(value), n, rel_tol=1e-9, abs_tol=1e-9) for n in ev.numbers):
            return "ok"
        if _said_in_words(slot, float(value), card, ev.text, context):
            return "ok"
        if ev.numbers and _talked_about(slot, ev.text):
            return "sink"
        return "missing" if slot.required else "drop"
    if kind == "color":
        resolution = resolve_visuals(page_words(ev.text, context), context)
        stated = _word_value(card, slot, ev.text, context, resolution, {})
        masked = mask_names(ev.text, context)
        if (
            card.op == "set_conditional_format"
            and _conditional_endpoint_color(
                slot,
                masked,
                find_colors(masked, context.palette),
            )
            is _AMBIGUOUS
        ):
            return "sink"
        if isinstance(stated, str) and stated.upper() != str(value).upper():
            return "sink"
        if stated is None and any(
            isinstance(_word_value(card, s, ev.text, context, resolution, {}), str)
            for s in card.slots
            if s.kind == "color" and s is not slot
        ):
            return "drop"
        if str(value).upper() in ev.colors:
            return "ok"
        if ev.colors and _talked_about(slot, ev.text):
            return "sink"
        return "missing" if slot.required else "drop"
    if kind == "text":
        if isinstance(value, list):  # theme colors: every one said
            return "ok" if value and all(str(v).upper() in ev.colors for v in value) else "sink"
        if card.op == "create_measure" and slot.name == "name":
            definition = new_measure_calculation(ev.text)
            if definition is not None and str(value).casefold() != definition[0].casefold():
                return "missing"
        if (
            card.op == "add_button"
            and slot.name == "text"
            and not _button_caption(str(value), ev.text, context)
        ):
            return "drop"
        if card.op == "set_field_display_name" and slot.name == "name":
            label = _column_label(ev.text)
            if label is not None and label.casefold() != str(value).casefold():
                return "sink"
        return "sink" if _negated_value(str(value), ev.text) else "ok"
    return "ok"


_A_LITTLE = re.compile(r"\b(?:a\s+(?:little|bit|touch|tad)|slightly|somewhat)\b", re.I)
_A_LOT = re.compile(r"\b(?:a\s+lot|much|far|way|strongly)\b", re.I)
_FIRST_PLACE = re.compile(
    r"\b(?:front|start|beginning|first|leftmost|top\s+of\s+the\s+tabs)\b", re.I
)
_LAST_PLACE = re.compile(r"\b(?:last|end|rightmost)\b", re.I)


def _said_in_words(
    slot: Slot, value: float, card: ToolCard, text: str, context: EditContext
) -> bool:
    """A number the words give without a digit: a place ("to the front" is 1, "the last page"
    is the number of pages) or, for a fraction slot (-1..1), a size ("a little" is at most a
    quarter or so, "a lot" more)."""
    if slot.kind == "integer" and slot.name in {"position", "place", "index"}:
        if _FIRST_PLACE.search(text) and value == 1:
            return True
        pages = any(s.kind == "page" for s in card.slots)
        if _LAST_PLACE.search(text) and pages and value == len(context.pages):
            return True
    if slot.maximum is not None and slot.maximum <= 1 and value != 0:
        if _A_LITTLE.search(text) and abs(value) <= 0.3:
            return True
        if _A_LOT.search(text) and 0.3 < abs(value) <= 0.8:
            return True
    return False


def _tables_of_columns(card: ToolCard, values: Mapping[str, Any], context: EditContext) -> set[str]:
    """The tables of the columns a step names ("rename the column StoreName": DimStore)."""
    found: set[str] = set()
    for slot in card.slots:
        value = values.get(slot.name)
        if slot.kind not in {"column", "field"} or value is None:
            continue
        if isinstance(value, Mapping) and value.get("table"):
            found.add(str(value["table"]))
            continue
        name = str(value.get("name") if isinstance(value, Mapping) else value).casefold()
        owners = [t for t, cols in context.tables.items() if name in {c.casefold() for c in cols}]
        if len(owners) == 1:
            found.add(owners[0])
    return found


def _implied(card: ToolCard, slot: Slot, values: Mapping[str, Any], context: EditContext) -> Any:
    """The value the step's own target gives a selector slot the words do not: the field well
    of a field being added (a measure goes to the values) or removed (the well it sits in); the
    table of the column the step names."""
    if slot.kind == "table":
        owners = _tables_of_columns(card, values, context)
        return next(iter(owners)) if len(owners) == 1 else None
    if slot.name == "axis" and {"horizontal", "vertical"} <= set(slot.choices):
        # "space the four cards evenly": visuals in one row are spaced across, in one column down
        listed = values.get("visuals")
        entries = [
            e for v in (listed if isinstance(listed, list) else []) if (e := context.visual(v))
        ]
        if len(entries) >= 2:
            if (
                max(e.cy for e in entries) - min(e.cy for e in entries)
                < min(e.height for e in entries) / 2
            ):
                return "horizontal"
            if (
                max(e.cx for e in entries) - min(e.cx for e in entries)
                < min(e.width for e in entries) / 2
            ):
                return "vertical"
        return None
    if slot.name != "role" or slot.kind != "enum":
        return None
    visual = values.get("visual")
    fields = values.get("fields") or values.get("field")
    entry = context.visual(str(visual)) if isinstance(visual, str) else None
    if entry is None or not fields:
        return None
    first = fields[0] if isinstance(fields, list) else fields
    name = str(first.get("name")) if isinstance(first, Mapping) else str(first)
    from . import edit_ops  # the role rules of the parser's own field edits

    if values.get("mode") == "remove":
        return edit_ops.role_of_field(entry, name)
    kind = "measure" if context.measure(name) is not None else "column"
    try:
        return edit_ops.default_role(entry, kind)
    except Exception:  # noqa: BLE001 - a visual with no well for it: no implied role
        return None


def _talked_about(slot: Slot, text: str) -> bool:
    """Whether the request talks about what a value slot sets ("a navy border": the border's
    color): its ``words``, or every word of its name that is not the kind of value it is."""
    if any(re.search(_phrase_pattern(p), text, re.I) for p in slot.words):
        return True
    alternatives = {
        "width": r"\bwide\b",
        "height": r"\b(?:tall|high)\b",
        "radius": r"\bround(?:ed|ing)?\b",
        "transparency": r"\btransparent\b",
        "decimals": r"\bdecimal(?:s|\s+places?)?\b",
        "font_size": r"\b(?:font|text)\s+size\b|\b(?:pt|points?)\b",
    }
    if slot.name in alternatives and re.search(alternatives[slot.name], text, re.I):
        return True
    words = _slot_words(slot)
    said = set(tokens(text))
    return bool(words) and words <= said


def _choice_backed(slot: Slot, value: str, text: str) -> bool | None:
    """True when the request says this choice; False when it says another of the slot's
    choices instead; None when it says none of them."""
    if re.search(_choice_pattern(value), text, re.I):
        return True
    if any(re.search(_choice_pattern(c), text, re.I) for c in slot.choices if c != value):
        return False
    # a long choice said by its telling word: "arrow icons" for ThreeColoredArrows
    words = [w for w in re.split(r"[\s_]+|(?<=[a-z])(?=[A-Z])", value) if w]
    if len(words) >= 3:
        said = set(tokens(text))
        telling = [w for w in words if len(w) > 3 and w.casefold() not in _WORD_NUMBERS]
        if any(set(tokens(w)) & said for w in telling[-1:]):
            return True
    return None


def _convert(
    slot: Slot,
    value: Any,
    card: ToolCard,
    lists: _Lists,
    context: EditContext,
    text: str,
) -> Any:
    kind = slot.kind
    if kind == "visual":
        entry = _nth(lists.visuals, value)
        if entry is None:
            return None
        fits = visual_types_of(card)
        if fits and entry.visual_type not in fits:
            return None
        return entry.id
    if kind == "visuals":
        items = value if isinstance(value, list) else [value]
        entries = [_nth(lists.visuals, i) for i in dict.fromkeys(items)]
        if not entries or any(e is None for e in entries):
            return None
        return [e.id for e in entries if e is not None]
    if kind == "page":
        page = _nth(lists.pages, value)
        return page.id if page is not None else None
    if kind in _FIELD_KINDS:
        items = value if isinstance(value, list) else [value]
        found: list[tuple[str, str, str]] = []
        for item in items:
            picked = _nth(lists.fields, item)
            if picked is None:
                return None
            found.append(picked)
        if not found:
            return None
        if kind == "measure" and any(f[0] != "measure" for f in found):
            return None
        if kind == "column" and any(f[0] != "column" for f in found):
            return None
        shape = _target_shape(card, slot)
        refs = [_field_value(kind, f[1], f[2], shape) for f in found]
        return refs if kind == "fields" else refs[0]
    if kind == "table":
        table: str | None = _nth(lists.tables, value)
        return table
    if kind == "enum":
        return value if isinstance(value, str) and value in slot.choices else None
    if kind == "bool":
        return value if isinstance(value, bool) else None
    if kind in {"number", "integer"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if kind == "integer" and not float(value).is_integer():
            return None
        number: float | int = int(value) if kind == "integer" else value
        if slot.minimum is not None and number < slot.minimum:
            return None
        if slot.maximum is not None and number > slot.maximum:
            return None
        return number
    if kind == "color":
        return _color_value(value, context)
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            return None
        said = value.strip()[:120]
        if _said(said, text):
            return said
        if (
            card.op == "sync_slicer"
            and slot.name in {"pages", "hidden_on"}
            and _ALL_PAGES.search(text)
        ):
            # "to every page": each page by its name
            names = {p.name.casefold() for p in context.pages}
            parts = [part.strip().casefold() for part in said.split(",") if part.strip()]
            return said if parts and all(part in names for part in parts) else None
        # a value the words give by the card tier's own rules ("the Total MTD Sales chart"
        # gives its one value to a calculation on it)
        worded = _word_value(
            card, slot, text, context, resolve_visuals(page_words(text, context), context), {}
        )
        if isinstance(worded, str) and worded.casefold() == said.casefold():
            return said
        return None
    return None


# --- the planner ---------------------------------------------------------------------------------


def _home_table(context: EditContext) -> str | None:
    """Where a new measure goes when the request does not say: where most measures live."""
    counts: dict[str, int] = {}
    for measure in context.measures:
        counts[measure.table] = counts.get(measure.table, 0) + 1
    if counts:
        return max(counts, key=lambda t: (counts[t], t.startswith("#")))
    return next(iter(context.tables), None)


def _setting_score(
    card: ToolCard,
    raw: PlanStepAnswer,
    piece: str,
    prompt: Prompt,
    context: EditContext,
) -> int:
    """Count requested setting values, without rewarding targets or implicit defaults."""
    evidence = gather_evidence([piece], context)
    score = 0
    for slot in _prompt_slots(card, piece, context):
        if (
            (slot.required and slot.kind != "bool")
            or slot.kind in _TARGET_KINDS | _FIELD_KINDS | {"table"}
            or slot.name not in raw.slots
        ):
            continue
        value = _convert(slot, raw.slots[slot.name], card, prompt.lists, context, piece)
        if value is None:
            continue
        if slot.kind == "enum" and _choice_backed(slot, str(value), evidence.text) is not True:
            continue
        # Visibility's required bool is still a requested setting. Its on/off polarity
        # distinguishes opposite clauses before retrieval or model answer order can.
        if _judge(slot, value, card, evidence, {}, set(), context) == "ok":
            score += 1
    return score


@dataclass(frozen=True)
class _FormulaRequest:
    text: str
    problem: str = ""


def _scoped_formula_request(
    piece: str, request: str, context: EditContext, target: str
) -> _FormulaRequest:
    """Only the current action's adjacent calculation detail can reach its formula call."""
    if not request:
        return _FormulaRequest(piece)
    names = Parser(context).names()
    parts = [
        (index, part)
        for index, sentence in enumerate(split_sentences(request))
        for part, _borrowed in split_clause_parts(sentence, names)
    ]

    def normalized(text: str) -> str:
        if target:
            # The parser quotes a new calculation's literal name; those inserted
            # delimiters do not make its otherwise identical current clause foreign.
            text = re.sub(rf"[\"']({re.escape(target)})[\"']", r"\1", text, flags=re.I)
        return clean(classify(text).body or text).strip(" .!?").casefold()

    needle = normalized(piece)
    anchors = [i for i, (_, part) in enumerate(parts) if needle == normalized(part)]
    if len(anchors) != 1:
        # Rewritten carried targets, duplicated clauses, or unrelated context are not
        # permission to borrow a different calculation from the original message.
        if any(
            normalized(part) != needle
            and (typed_expression(part) is not None or describes_calculation(part))
            for _, part in parts
        ):
            return _FormulaRequest(
                piece,
                "I could not attach the calculation detail to one requested calculation. "
                "Name that calculation and its formula together.",
            )
        return _FormulaRequest(piece)
    at = anchors[0]
    sentence_index, original = parts[at]
    text = [original]
    formula_actions = sum(
        1
        for index, part in parts
        if index == sentence_index
        and re.search(r"\b(?:create|add|new|make|define|build|update|change)\b", part, re.I)
        and re.search(r"\b(?:columns?|measures?|formula|dax)\b", part, re.I)
    )
    for _index, later in parts[at + 1 :]:
        info = classify(later)
        if info.kind in {SentenceKind.QUESTION, SentenceKind.WHAT_IF}:
            break
        if re.search(
            r"\b(?:new|another)\s+(?:(?:calculated|dax)\s+)?(?:measure|column)\b"
            r"|^(?:calculate|compute|use)\s+(?:a|an)\s+(?:(?:calculated|dax)\s+)?(?:measure|column)\b",
            clean(later),
            re.I,
        ):
            break
        calculation = typed_expression(later) is not None or describes_calculation(later)
        continuation = info.kind is SentenceKind.OTHER or re.match(
            r"^(?:for\s+(?:every|each)\s+row\b|calculate\b|compute\b|use\b)",
            clean(later),
            re.I,
        )
        if not calculation or not continuation:
            break
        if formula_actions > 1 and not (target and _said(target, later)):
            return _FormulaRequest(
                piece,
                "Which new calculation should the following formula detail apply to? "
                "Name the calculation and its formula together.",
            )
        text.append(later)
    return _FormulaRequest(" ".join(text))


@dataclass(frozen=True)
class _ColumnBinaryRequest:
    table: str
    left: str
    op: str
    right: str


def _column_binary_request(
    text: str, values: Mapping[str, Any], context: EditContext, target: str
) -> _ColumnBinaryRequest | str | None:
    """Bind an explicit two-column row operation, without guessing from a new column name."""
    table = values.get("table")
    if not isinstance(table, str) or table not in context.tables:
        return None
    text = _without_name(clean(text), target)
    mentions: list[tuple[int, int, str, str]] = []
    for owner, columns in context.tables.items():
        quoted_owner = owner.replace("'", "''")
        owner_pattern = rf"(?:'{re.escape(quoted_owner)}'|{re.escape(owner)})"
        for column in columns:
            pattern = rf"(?<!\w){owner_pattern}\s*\[\s*{re.escape(column)}\s*\]"
            mentions.extend(
                (m.start(), m.end(), owner, column) for m in re.finditer(pattern, text, re.I)
            )
    for column in context.tables[table]:
        pattern = rf"(?<![\w\[])\[?{re.escape(column)}\]?(?!\w)"
        for match in re.finditer(pattern, text, re.I):
            if not any(
                match.start() < end and start < match.end() for start, end, _, _ in mentions
            ):
                mentions.append((match.start(), match.end(), table, column))
    mentions.sort()
    operations = {
        "minus": "-",
        "less": "-",
        "plus": "+",
        "times": "*",
        "multiplied by": "*",
        "divided by": "/",
        "-": "-",
        "+": "+",
        "*": "*",
        "/": "/",
    }
    contracts: list[_ColumnBinaryRequest] = []
    for left, right in zip(mentions, mentions[1:], strict=False):
        between = " ".join(text[left[1] : right[0]].casefold().split())
        if between in operations and left[2] == right[2] == table:
            contracts.append(_ColumnBinaryRequest(table, left[3], operations[between], right[3]))
    if len(contracts) > 1:
        return (
            "This row calculation names several operations. "
            "Type the complete formula so I keep their order."
        )
    return contracts[0] if contracts else None


def _column_binary_problem(expression: str, requested: _ColumnBinaryRequest) -> str | None:
    from .dax_syntax import Binary, ColRef, DaxParseError, Name, Node, VarBlock, parse_expression

    try:
        node = parse_expression(expression)
    except DaxParseError:
        return None  # the ordinary syntax/name check reports its own error
    aliases: dict[str, Node] = {}
    if isinstance(node, VarBlock):
        aliases = {name.casefold(): value for name, value in node.variables}
        node = node.body

    def reference(item: Node) -> tuple[str, str] | None:
        seen: set[str] = set()
        while isinstance(item, Name) and item.name.casefold() in aliases:
            key = item.name.casefold()
            if key in seen:
                return None
            seen.add(key)
            item = aliases[key]
        if isinstance(item, ColRef):
            return ((item.table or requested.table).casefold(), item.column.casefold())
        return None

    if (
        isinstance(node, Binary)
        and node.op == requested.op
        and reference(node.left) == (requested.table.casefold(), requested.left.casefold())
        and reference(node.right) == (requested.table.casefold(), requested.right.casefold())
    ):
        return None
    return (
        f"The requested row calculation is {requested.table}[{requested.left}] {requested.op} "
        f"{requested.table}[{requested.right}]. The draft changed its operands or operator; "
        "keep the explicitly requested row calculation."
    )


class EditPlanner:
    """Plans what the parser could not read. One instance per request."""

    def __init__(
        self,
        context: EditContext,
        model: PlannerModel | None,
        *,
        cards: Sequence[ToolCard] | None = None,
        progress: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        check_dax_expression: Callable[[str, EditContext], list[str]] = check_dax,
        check_m_expression: Callable[[str, EditContext], list[str]] = check_m,
        pattern_catalog: Callable[[], dax_patterns.PatternCatalog] | None = None,
        pattern_preview: Callable[[dax_patterns.BuiltPattern], tuple[str, str]] | None = None,
        formula_request: str = "",
        budget: int | None = None,
        seconds: float = TURN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.context = context
        self.model = model
        self.cards = tuple(available_cards() if cards is None else cards)
        self.progress = progress or (lambda _label: None)
        self.cancelled = cancelled or (lambda: False)
        self.check_dax = check_dax_expression
        self.check_m = check_m_expression
        self.pattern_catalog = pattern_catalog
        self.pattern_preview = pattern_preview
        self.formula_request = formula_request
        self.budget = PROMPT_BUDGET if budget is None else budget
        self.clock = clock
        self.deadline = clock() + seconds
        self.calls = 0
        self.prompt_bytes = 0
        self.prompt: Prompt | None = None  # the choose call's prompt (evals read its lists)

    # -- helpers -------------------------------------------------------------------------------

    def _seconds_left(self) -> int:
        return int(min(MODEL_SECONDS, self.deadline - self.clock()))

    def shortlist(self, pieces: Sequence[str]) -> list[ToolCard]:
        """The cards for every piece of the request, round robin (each piece's best first).
        A card far behind its piece's best is left out: its lines would push fields and
        visuals out of the prompt for a card the request barely touches."""
        per_piece = []
        for piece in pieces:
            found = [
                s
                for s in retrieve(piece, self.context, self.cards)
                if _asked_for(s.card, piece, self.context)
            ]
            target_type = _target_type(piece, self.context)
            if target_type is not None:
                found = [
                    s
                    for s in found
                    if not visual_types_of(s.card) or target_type in visual_types_of(s.card)
                ]
            if _removes_a_field_from_a_visual(piece, self.context):
                # "take Employee Name out of the table", "remove Rank from the chart": the
                # field wells, not a bookmark, a group or the visual itself
                field_cards = [s for s in found if s.card.op == "set_fields"]
                if field_cards:
                    found = field_cards
            if re.search(r"\b(?:filter|limit|restrict|only\s+show|exclude)\b", piece, re.I):
                named = named_visuals(piece, self.context)
                scope = (
                    "report"
                    if re.search(r"\breport\b", piece, re.I) or _ALL_PAGES.search(piece)
                    else "visual"
                    if named or self.context.selection
                    else "page"
                    if re.search(r"\bpage\b", piece, re.I)
                    else None
                )
                if scope is not None:
                    found = [
                        s
                        for s in found
                        if s.card.op
                        not in {"set_visual_filter", "set_page_filter", "set_report_filter"}
                        or s.card.op == f"set_{scope}_filter"
                    ]
            if re.search(r"\b(?:filtering|highlighting|interactions?)\b", piece, re.I):
                if not re.search(r"\b(?:reset|restore)\b", piece, re.I):
                    found = [s for s in found if s.card.op != "reset_interactions"]
                if not re.search(r"\bsync", piece, re.I):
                    found = [s for s in found if s.card.op not in {"sync_slicer", "unsync_slicer"}]
            if found:
                floor = RELATIVE_FLOOR * found[0].score
                found = [s for i, s in enumerate(found) if i < MIN_CARDS or s.score >= floor]
                precise = {
                    "add_button",
                    "set_conditional_format",
                    "set_field_display_name",
                    "set_visual_filter",
                    "set_interaction",
                    "set_fields",
                }
                if len(found) == 1 or (
                    found[0].card.op in precise
                    and found[0].cue >= STRONG_CUE
                    and found[0].score - found[1].score >= 4
                ):
                    found = found[:1]
            per_piece.append(found)
        chosen: list[ToolCard] = []
        for rank in range(MAX_CARDS):
            for found in per_piece:
                if rank < len(found) and found[rank].card not in chosen:
                    chosen.append(found[rank].card)
        return chosen[:MAX_CARDS]

    # -- the plan ------------------------------------------------------------------------------

    def plan(self, unparsed: Sequence[str]) -> PlanOutcome:
        requested = [p.strip() for p in unparsed if p.strip()]
        read_only = [
            p
            for p in requested
            if classify(p).kind
            in {
                SentenceKind.QUESTION,
                SentenceKind.WHAT_IF,
            }
            or re.fullmatch(
                r"(?:thanks|thank\s+you|(?:(?:it|that|this)\s+)?looks?\s+"
                r"(?:good|great|fine|perfect)|(?:all\s+)?(?:good|great|perfect)|ok(?:ay)?)"
                r"(?:\s*[,;]\s*(?:thanks|thank\s+you))?\s*[.!]*",
                p,
                re.I,
            )
        ]
        requested = [p for p in requested if p not in read_only]
        pieces = requested[:MAX_STEPS]
        left_out = tuple(read_only + requested[MAX_STEPS:])
        if not pieces:
            return PlanOutcome("failed", left_out=left_out, reason="not an edit request")
        # The model may choose only among supported tools; that does not make a tool a
        # substitute for a refused request (a visible title is not alt text, for example).
        # Reuse the parser's contextual boundary before retrieval or any model call. It
        # protects real field/caption names and separates mixed clauses before checking
        # styling, whose words may also belong to supported conditional/number formats.
        parser = Parser(self.context)
        blocked = frozenset(unsupported_keys())
        for index, piece in enumerate(requested):
            parsed = parse_edit_message(piece, self.context)
            refused = next(
                (
                    c
                    for c in parsed.clauses
                    if c.kind is ParseKind.UNSUPPORTED and c.capability in blocked
                ),
                None,
            )
            if refused is None:
                refused = next(
                    (c for p in parsed.unparsed if (c := parser.styling(p)) is not None), None
                )
            if refused is None:
                # Direct planner callers can supply a request whose verb the message
                # classifier does not recognize ("end the y axis at ...", "sign in").
                direct = parser.clause(piece)
                if direct is None:
                    direct = parser.styling(piece)
                if (
                    direct is not None
                    and direct.kind is ParseKind.UNSUPPORTED
                    and direct.capability in blocked
                ):
                    refused = direct
            if refused is not None:
                remaining = tuple(p for i, p in enumerate(requested) if i != index)
                remaining += tuple(
                    c.text
                    for c in parsed.clauses
                    if c.kind in {ParseKind.APPLY, ParseKind.MODEL_EDIT, ParseKind.CLARIFY}
                )
                remaining += tuple(p for p in parsed.unparsed if p != refused.text)
                return self._outcome(
                    "unsupported", remaining, clause=refused, reason=refused.reason
                )
        cards = self.shortlist(pieces)
        if not cards:
            return PlanOutcome("failed", left_out=left_out, reason="no tool fits the words")
        for piece in pieces:
            ranked = retrieve(piece, self.context, cards)
            if ranked and ranked[0].card.op in {"group_visuals", "ungroup_visuals", "set_group"}:
                grounded = ground_group_targets(self.context, piece, ranked[0].card, {})
                if isinstance(grounded, Clause):
                    return self._outcome("clarify", left_out, clause=grounded)
        text = (
            pieces[0]
            if len(pieces) == 1
            else " ".join(f"{n}) {p.rstrip('.')}." for n, p in enumerate(pieces, 1))
        )
        try:
            safe_text = (
                m_prompt_text(pieces[0])
                if len(pieces) == 1
                else " ".join(
                    f"{n}) {m_prompt_text(p).rstrip('.')}." for n, p in enumerate(pieces, 1)
                )
            )
            prompt = build_prompt(safe_text, self.context, cards, budget=self.budget)
        except TooLarge as error:
            question = Clause(" ".join(pieces), ParseKind.CLARIFY, question=str(error))
            return PlanOutcome("clarify", clause=question, left_out=left_out, reason="too large")
        self.prompt_bytes = prompt.size
        self.prompt = prompt
        if self.model is None or self.cancelled():
            return PlanOutcome("failed", left_out=left_out, reason="no model")
        self.progress("Asking the local AI to plan your request (up to a minute)")
        try:
            self.calls += 1
            answer = self.model.generate(
                PLAN_SYSTEM,
                prompt.user,
                answer_model(prompt.schema),
                max_output_tokens=CHOOSE_TOKENS,
                timeout_seconds=self._seconds_left(),
                repair_attempts=0,
            )
        except Exception as error:  # noqa: BLE001 - any failure means suggestions, never an edit
            return self._outcome("failed", left_out, reason=f"model: {str(error)[:160]}")
        if not isinstance(answer, PlanAnswer):
            return self._outcome("failed", left_out, reason="the model answered another shape")
        return self._from_answer(answer, prompt, text, pieces, left_out)

    def _outcome(
        self,
        kind: Literal["plan", "clarify", "unsupported", "failed"],
        left_out: tuple[str, ...],
        **values: Any,
    ) -> PlanOutcome:
        return PlanOutcome(
            kind, left_out=left_out, calls=self.calls, prompt_bytes=self.prompt_bytes, **values
        )

    def _from_answer(
        self,
        answer: PlanAnswer,
        prompt: Prompt,
        text: str,
        pieces: Sequence[str],
        left_out: tuple[str, ...],
    ) -> PlanOutcome:
        whole = " ".join(pieces)
        if answer.kind == "unsupported" or (answer.kind == "plan" and not answer.steps):
            refused = detect_unsupported(whole)
            if refused is not None:
                clause = Clause(
                    whole,
                    ParseKind.UNSUPPORTED,
                    capability=refused.key,
                    reason=refused.reason or f"Studio cannot do {refused.title.lower()} yet.",
                    desktop_steps=refused.desktop_steps,
                )
                return self._outcome("unsupported", left_out, clause=clause)
            return self._outcome("failed", left_out, reason=f"model said {answer.kind}")
        if answer.kind == "unclear":
            return self._outcome("failed", left_out, reason="model said unclear")
        steps: list[PlanStep] = []
        step_order: list[int] = []
        assigned: set[int] = set()
        rankings = [
            {
                s.card.op: s.score
                for s in retrieve(piece, self.context, self.cards, limit=len(self.cards))
            }
            for piece in pieces
        ]
        notes: list[str] = []
        evidence = gather_evidence(pieces, self.context)
        # one step per piece of the request at most: a step more is a change nobody asked for
        allowed = min(len(pieces), MAX_STEPS)
        surplus = 0
        question: Clause | None = None
        for raw in answer.steps[:MAX_STEPS]:
            if len(steps) >= allowed:
                surplus += 1
                continue
            if not 1 <= raw.tool <= len(prompt.cards):
                notes.append("I left out one step: a tool that was not offered.")
                continue
            card = prompt.cards[raw.tool - 1]
            available = [
                i
                for i in range(len(pieces))
                if i not in assigned and _asked_for(card, pieces[i], self.context)
            ]
            # A named target can boost a different clause's retrieval score. Requested
            # settings are stronger evidence; an offered enum default is not a request.
            at = max(
                available,
                key=lambda i: (
                    _setting_score(card, raw, pieces[i], prompt, self.context),
                    rankings[i].get(card.op, 0),
                ),
                default=0,
            )
            piece = pieces[at]
            scoped = gather_evidence([piece], self.context)
            if card.op == "sync_slicer" and _sync_source_text(piece) != piece:
                source_evidence = gather_evidence([_sync_source_text(piece)], self.context)
                sources = _sync_visuals(piece, self.context)
                scoped = replace(
                    scoped,
                    visuals=frozenset(e.id for e in sources)
                    if len(sources) == 1
                    else source_evidence.visuals,
                    candidates=tuple(e.id for e in sources) or source_evidence.candidates,
                    selection=source_evidence.selection,
                )
            if (
                not scoped.visuals
                and len(evidence.visuals) == 1
                and re.search(r"\b(?:its|it|that|same|their|them)\b", piece, re.I)
            ):
                scoped = replace(scoped, visuals=evidence.visuals)
            read = read_step(raw, prompt, piece, self.context, scoped)
            if isinstance(read, str):
                notes.append(f"I left out one step: {read}.")
                continue
            label = read.card.label.lower()
            if not _asked_in(read.card, pieces, self.context):
                notes.append(f"I left out a step to {label}: your request does not ask for that.")
                continue
            if _negated(read.card, whole):
                notes.append(f"I left out a step to {label}: you asked me not to.")
                continue
            made = self._complete(read, piece, prompt, scoped)
            if isinstance(made, Clause):
                question = question or made
                notes.append(made.question)
                continue
            if isinstance(made, str):
                notes.append(f"I could not {label}: {made}")
                continue
            if any(s.operation == made.operation for s in steps):
                continue  # the same change twice
            steps.append(made)
            step_order.append(at)
            assigned.add(at)
        if surplus:
            more = "one more step" if surplus == 1 else f"{surplus} more steps"
            notes.append(f"I left out {more} than you asked for.")
        if not steps and question is not None:
            # nothing ready to apply, one thing to ask: the question, with its choices
            return self._outcome("clarify", left_out, clause=question)
        if not steps:
            reason = "; ".join(notes) or "no usable step"
            return self._outcome("failed", left_out, reason=reason[:300])
        ordered = tuple(
            s for _, s in sorted(zip(step_order, steps, strict=True), key=lambda item: item[0])
        )
        return self._outcome("plan", left_out, steps=ordered, notes=tuple(notes))

    # -- one step --------------------------------------------------------------------------------

    def _complete(
        self, read: _Read, text: str, prompt: Prompt, evidence: Evidence | None = None
    ) -> PlanStep | Clause | str:
        """The step with its missing slots taken from the words or the selection, a formula
        drafted and checked, then built and validated; a question when a target, a page or a
        required choice is missing. A value that contradicts the request sinks the step:
        with it left out, the operation's default would stand in for what the request said
        (dollars for "in €"). A value the request does not give at all is left out, and the
        step says so."""
        if read.problems:
            return f"{read.problems[0]}."
        ev = evidence or gather_evidence([text], self.context)
        card, values = read.card, dict(read.values)
        resolution = resolve_visuals(
            page_words(_sync_source_text(text), self.context), self.context
        )
        used = {str(v) for s in card.slots if s.kind == "visual" and (v := values.get(s.name))}
        for slot in sorted(read.missing, key=lambda s: s.kind in {"enum", "table"}):
            value = self._default(card, slot, text, resolution, used)
            if value is None and slot.kind in {"enum", "table"}:
                value = _implied(card, slot, values, self.context)
            if value is None and slot.kind == "visual" and len(ev.visuals - used) == 1:
                value = next(iter(ev.visuals - used))
            if value is None and slot.kind == "text":
                value = _word_value(card, slot, text, self.context, resolution, {})
                if value is _AMBIGUOUS:
                    value = None
            if value is not None:
                values[slot.name] = value
                read.missing.remove(slot)
                if slot.kind == "visual":
                    used.add(str(value))
        for slot in read.retarget:
            value = self._default(card, slot, text, resolution, used)
            if value is None:
                return f"the {slot.name.replace('_', ' ')} it gave are not the ones you named."
            values[slot.name] = value
        offered = {s.name for s in _prompt_slots(card, text, self.context)}
        # An optional list of visuals ("the visuals that react", "hidden in this bookmark")
        # takes only visuals the words name, never the selection, and only when it is the
        # card's one list or the words talk about it; no visual goes in two lists.
        lists = [s for s in card.slots if s.kind == "visuals" and not s.required]
        listed = {str(v) for s in lists for v in (values.get(s.name) or [])}
        for slot in card.slots:
            if (
                slot.name in offered
                and slot.name not in values
                and not slot.required
                and (
                    slot.kind == "bool"
                    or (slot.kind == "visuals" and (len(lists) == 1 or _talked_about(slot, text)))
                    or (
                        slot.kind == "enum"
                        and (
                            slot.name in {"mode", "kind"}
                            or _talked_about(slot, text)
                            or (card.op == "set_conditional_format" and slot.name in _ICON_PLACES)
                            or (card.op == "set_conditional_format" and slot.name == "icon_set")
                        )
                    )
                    or (slot.kind == "text" and re.search(r"field|column", slot.name))
                    or (slot.name == "threshold" and values.get("style") == "rules")
                    or (slot.name == "values" and card.op in _SET_FILTER_OPS)
                )
            ):
                if slot.kind == "visuals":
                    named = [
                        e.id
                        for e in named_visuals(text, self.context)
                        if e.id not in used and e.id not in listed
                    ]
                    value = named or None
                    listed |= set(named)
                elif slot.kind == "enum":
                    value = self._default(card, slot, text, resolution, used)
                else:
                    value = _word_value(card, slot, text, self.context, resolution, {})
                if value is None and slot.name == "values":
                    value = self._filter_values(text, values)
                if value is not None and value is not _AMBIGUOUS:
                    values[slot.name] = value
            # the page something new goes on is the one on screen; a new measure lives where
            # the others do (a page a card points at, "target_page", has no default)
            if slot.name not in values and (
                (slot.kind == "page" and slot.name == "page")
                or (slot.kind == "table" and slot.name == "table")
            ):
                value = self._default(card, slot, text, resolution, used)
                if value is not None:
                    values[slot.name] = value
        if card.op == "add_button" and not values.get("action"):
            # what clicking does, as the destination the words name gives it
            for slot_name, action in (
                ("target_page", "page"),
                ("target_bookmark", "bookmark"),
                ("url", "web_url"),
            ):
                if values.get(slot_name) and card.slot("action") is not None:
                    values["action"] = action
                    break
        constrained = constrain_edit_request(self.context, text, card, values)
        if constrained.clarification is not None:
            if constrained.choices and (icon_slot := card.slot("icon_set")) is not None:
                options = [
                    (choice.fill["icon_set"], choice.label) for choice in constrained.choices
                ]
                return _value_question(
                    self.context,
                    card,
                    constrained.values,
                    icon_slot,
                    text,
                    options,
                    source="model",
                    question=constrained.clarification.question,
                )
            return constrained.clarification
        values = constrained.values
        grounded_relationship = ground_relationship(self.context, text, card, values)
        if isinstance(grounded_relationship, Clause):
            return grounded_relationship
        values = grounded_relationship
        notes: list[str] = list(read.dropped)
        if card.op == "sync_slicer":
            synced = self._sync_pages(values, text)
            if synced is not None:
                return synced
        if card.op in _SET_FILTER_OPS:
            said_values = self._filter_values(text, values)
            if said_values is not None:
                # Recover every category the request names, including repeated field
                # nouns, even when the model supplied only one of the categories.
                values["values"] = said_values
        if (
            card.op in _SET_FILTER_OPS
            and values.get("field") is not None
            and not any(
                values.get(name) not in (None, "", [])
                for name in ("values", "condition", "top", "bottom")
            )
        ):
            # a filter on a field with no value said: which values is the user's to say
            field_name = _field_name_of(values.get("field"))
            keep = "leave out" if values.get("exclude") else "keep"
            return Clause(
                text,
                ParseKind.CLARIFY,
                question=f"Which {field_name} values should the filter {keep}? Name them as "
                "they appear in the report.",
            )
        if (
            card.op == "set_conditional_format"
            and values.get("style") == "icons"
            and not values.get("icon_set")
            and (icon_slot := card.slot("icon_set")) is not None
        ):
            kinds = _icon_kinds(card, text)
            if len(kinds) > 1:  # "colored arrows": three, four or five of them
                return self._which_value(card, values, icon_slot, text, prompt, kinds)
        if (
            card.op == "set_conditional_format"
            and values.get("style") == "rules"
            and isinstance(values.get("threshold"), int | float)
            and not (values.get("below_color") or values.get("above_color"))
        ):
            # a rule with no color said: the colors are the user's to say
            at = plain_number(float(values["threshold"]))
            return Clause(
                text,
                ParseKind.CLARIFY,
                question=f"Which color should the values below {at} get, and which the values "
                f"from {at} up? Name one or both.",
            )
        grounded = ground_group_targets(self.context, text, card, values)
        if isinstance(grounded, Clause):
            return grounded
        values = grounded
        grounded = ground_role_filter(
            self.context,
            text,
            card,
            values,
            typed=typed_expression(text, "dax"),
        )
        if isinstance(grounded, Clause):
            return grounded
        values = grounded
        formula_text = text
        if card.op in {"create_calculated_column", "create_measure", "update_measure"}:
            request = _scoped_formula_request(
                text, self.formula_request, self.context, _target_name(card, values)
            )
            if request.problem:
                return Clause(text, ParseKind.CLARIFY, question=request.problem)
            formula_text = request.text
        for slot in card.slots:
            if slot.kind not in {"dax", "m"} or slot.name in values:
                continue
            # a role whose words say what it sees needs its row filter: a role without one
            # sees every row
            sees = card.op in _ROLE_OPS and slot.name == "filter" and _ROLE_SEES.search(text)
            if not slot.required and not _wants_formula(formula_text) and not sees:
                continue
            if (
                card.op == "create_measure"
                and slot.kind == "dax"
                and typed_expression(formula_text, slot.kind) is None
                and not describes_calculation(
                    _without_name(formula_text, _target_name(card, values))
                )
            ):
                # a name and no calculation ("Sales YTD that I can use later"): nothing to
                # draft from but the name, so the user says what it calculates
                measure_name = _target_name(card, values) or "the new measure"
                return Clause(
                    text,
                    ParseKind.CLARIFY,
                    question=f"What should [{measure_name}] calculate? Say it in words "
                    '("the sum of SalesAmount") or type the formula '
                    '("= SUM(FactSales[SalesAmount])").',
                )
            drafted = self._formula(card, slot, values, formula_text)
            if isinstance(drafted, str):
                return drafted
            expression, said = drafted
            values[slot.name] = expression
            notes += said
            if slot in read.missing:
                read.missing.remove(slot)
        if card.op in _ROLE_OPS and values.get("filter"):
            problems = validate_role_expression(str(values["filter"]), self.context)
            if problems:
                return Clause(text, ParseKind.CLARIFY, question=problems[0])
            # the table a row filter is on is the one its columns belong to
            tables = _formula_tables(str(values["filter"]), self.context)
            if len(tables) != 1 and values.get("table"):
                tables = [str(values["table"])]  # (no one table in the formula: the one given)
            if len(tables) != 1:
                return Clause(
                    text,
                    ParseKind.CLARIFY,
                    question="Which table should the role's filter be on? Name it.",
                )
            values["table"] = tables[0]
        if _changes_nothing(card, values):
            return "the request does not say what the new setting is."
        partner = _lacks_its_partner(card, values)
        if partner is not None and any(s.kind == "visual" for s in read.missing):
            # neither the visual nor what its calculation is of: both are the user's to say
            could_be = [
                candidate_label(entry, self.context)
                for vid in ev.candidates[:6]
                if (entry := self.context.visual(vid)) is not None
            ]
            could_be_text = f" It could be: {'; '.join(could_be)}." if could_be else ""
            return Clause(
                text,
                ParseKind.CLARIFY,
                question=f"Which visual, and which of its fields, should I use? ({card.label})"
                + could_be_text,
            )
        if partner is not None:
            return self._which_partner(card, values, partner, text)
        if read.missing:
            first = read.missing[0]
            if first.kind == "visual":
                return self._which_visual(card, values, first, text, prompt, ev)
            if any(s.kind in {"column", "field", "table"} for s in read.missing):
                asked = self._which_table(card, values, read.missing, text)
                if asked is not None:
                    return asked
            if first.kind in {"page", "enum"}:
                return self._which_value(card, values, first, text, prompt)
            if first.kind == "visuals" and ev.candidates:
                # "the two cards" on a page with five: which ones, by name and place
                shown = [
                    candidate_label(entry, self.context)
                    for vid in ev.candidates[:8]
                    if (entry := self.context.visual(vid)) is not None
                ]
                return Clause(
                    text,
                    ParseKind.CLARIFY,
                    question=f"Which visuals should I {card.label.lower()}? Name them: "
                    + "; ".join(shown)
                    + ".",
                )
            # a required value the words do not give: asked, never guessed
            return Clause(
                text,
                ParseKind.CLARIFY,
                question=f"To {card.label.lower()}, I need to know {first.description}. "
                "Say it and I will prepare the change.",
            )
        return build_step(card, values, self.context, source="model", notes=notes, request=text)

    def _which_partner(
        self, card: ToolCard, values: Mapping[str, Any], partner: Slot, text: str
    ) -> Clause:
        """The question for a slot another one needs ("a running total" of which field): one
        ready choice per field the target visual shows, each validated."""
        entry = self.context.visual(str(values.get("visual", "")))
        names = list(dict.fromkeys(name for _kind, _table, name in entry.bindings)) if entry else []
        choices: list[Choice] = []
        for name in names[:6]:
            step = build_step(card, {**values, partner.name: name}, self.context, source="model")
            if isinstance(step, PlanStep):
                choices.append(Choice(name[:160], ops=(step.operation,), summary=step.summary))
        return Clause(
            text,
            ParseKind.CLARIFY,
            question=f"Which field should I use for this? ({card.label}: {partner.description})",
            choices=tuple(choices),
        )

    def _sync_pages(self, values: dict[str, Any], text: str) -> Clause | None:
        """The pages a synced slicer goes to and those it is hidden on, from the words: the
        pages named past the slicer, or every other page for "every page"; "hidden there"
        hides it on those pages. A question when the words name none (or no other page
        exists), never a guess."""
        source = self.context.visual(str(values.get("visual", "")))
        source_page = source.page_id if source else self.context.active_page
        others = [p for p in self.context.pages if p.id != source_page]
        if _ALL_PAGES.search(text):
            destinations = others
        else:
            destinations = [
                p for p in find_pages_by_name(text, self.context) if p.id != source_page
            ]
            said = [n.strip() for n in str(values.get("pages") or "").split(",") if n.strip()]
            destinations += [p for p in others if p.name in said and p not in destinations]
        hide = _HIDDEN_THERE.search(text)
        before_hide = text[max(0, hide.start() - 12) : hide.start()] if hide is not None else ""
        if hide is not None and not _NEGATION.search(before_hide):
            after = find_pages_by_name(text[hide.end() :], self.context)
            hidden = [p for p in after if p.id != source_page] or (
                destinations if _THERE.search(text[hide.start() :]) else []
            )
            if not hidden:
                return self._sync_question(
                    values,
                    text,
                    destinations or others,
                    "hidden_on",
                    "Which pages should the synced slicer be on but hidden?",
                )
            values["hidden_on"] = ", ".join(dict.fromkeys(p.name for p in hidden))
            shown = [p for p in destinations if p not in hidden]
            if shown:
                values["pages"] = ", ".join(dict.fromkeys(p.name for p in shown))
            else:
                values.pop("pages", None)
            return None
        if destinations:
            values["pages"] = ", ".join(dict.fromkeys(p.name for p in destinations))
            return None
        values.pop("pages", None)
        return self._sync_question(
            values, text, others, "pages", "Which pages should the slicer be synced to?"
        )

    def _sync_question(
        self,
        values: Mapping[str, Any],
        text: str,
        pages: Sequence[PageEntry],
        slot: str,
        question: str,
    ) -> Clause:
        card = next((c for c in self.cards if c.op == "sync_slicer"), None)
        choices: list[Choice] = []
        for page in pages[:6]:
            if card is None:
                break
            step = build_step(card, {**values, slot: page.name}, self.context, source="model")
            if isinstance(step, PlanStep):
                choices.append(
                    Choice(_page_label(page), ops=(step.operation,), summary=step.summary)
                )
        if not pages:
            question += " This report has no other page yet: add the page first."
        return Clause(text, ParseKind.CLARIFY, question=question, choices=tuple(choices))

    def _filter_values(self, text: str, values: Mapping[str, Any]) -> str | None:
        """The categories said right before the filter's field ("the Store channel", "the
        Store and Online channels"): each a capitalized name or a number, as written. None
        when anything else stands there ("our biggest channel", "the channel"): the question
        asks for the values, never a word of the sentence."""
        if values.get("count") is not None or values.get("condition") is not None:
            return None
        if re.search(r"\b(?:top|bottom)\s+\d", text, re.I):
            return None
        name = _field_name_of(values.get("field"))
        if not name:
            return None
        labels = "|".join(r"\s+".join(map(re.escape, form.split())) for form in _label_forms(name))
        known = _known_names(self.context)
        found: list[str] = []
        for match in re.finditer(rf"(?<![\w-])(?:{labels})(?:e?s)?(?![\w-])", text, re.I):
            words = re.findall(r"[A-Za-z0-9][\w'&.-]*|,", text[: match.start()])
            items: list[str] = []
            group: list[str] = []
            for word in reversed(words):
                low = word.casefold().rstrip(".")
                if word == "," or low in {"and", "or", "the", "&"}:
                    if group:
                        items.append(" ".join(reversed(group)))
                        group = []
                    continue
                if (word[0].isupper() or word[0].isdigit()) and low not in _NOT_A_VALUE:
                    group.append(word.rstrip("."))
                    continue
                break
            if group:
                items.append(" ".join(reversed(group)))
            found += reversed(items)
        said = list(dict.fromkeys(found))
        if not said or any(item.casefold() in known for item in said):
            return None  # a name of the report ("Rank and the Online channel"): not a value
        return ", ".join(said)

    def _default(
        self, card: ToolCard, slot: Slot, text: str, resolution: Any, used: set[str] | None = None
    ) -> Any:
        """A value the words or the selection give for a slot the model left out or filled
        with something the request does not back (never a guess)."""
        context = self.context
        taken = used or set()
        types = visual_types_of(card)
        if slot.kind == "visual":
            if card.op == "sync_slicer":
                sources = [e for e in _sync_visuals(text, context) if e.id not in taken]
                if len(sources) == 1:
                    return sources[0].id
            if card.op == "swap_visuals":
                named = [e for e in named_visuals(text, context) if e.id not in taken]
                if len(named) + len(taken) == 2 and named:
                    return named[0].id
            if resolution.ok and resolution.explicit and resolution.how != "selection":
                fits = [
                    e
                    for e in resolution.entries
                    if e.id not in taken and (not types or e.visual_type in types)
                ]
                return fits[0].id if len(fits) == 1 else None
            selected = context.visual(context.selection) if context.selection else None
            if selected is None or selected.id in taken:
                return None
            if types and selected.visual_type not in types:
                return None
            return selected.id
        if slot.kind == "visuals":
            named = named_visuals(text, context)
            if named:
                rest = [e.id for e in named if e.id not in taken]
                return rest or None
            # the selection stands in only for a list the card cannot do without; an optional
            # list left out means "all of them", which the words did not narrow
            if slot.required and context.selection and context.selection not in taken:
                return [context.selection]
            return None
        if slot.kind == "page":
            if _placement(card) and card.slot("target_page") is not None:
                # a button: where it goes and where it points, each from its own words
                _placed, targets, _either = _button_pages(text, context)
                if slot.name == "target_page":
                    return targets[0].id if len({p.id for p in targets}) == 1 else None
                page = _placed_page(card, text, context)
                return page.id if page is not None else None
            page = named_page(text, context)
            if page is None and _page_by_default(card, text):
                page = _default_page(context)
            return page.id if page is not None else None
        if slot.kind == "table":
            # a new measure goes where the others live; a column or a table names its own
            local_text = text
            if card.op.endswith("_measure"):
                body = typed_expression(text, "dax")
                if body:
                    local_text = text.replace(body, "")
            named_tables = [t for t in context.tables if _name_spans(local_text, t)]
            if len(named_tables) == 1:
                return named_tables[0]
            return _home_table(context) if card.op.endswith("_measure") else None
        if slot.kind in {"field", "measure", "column"}:
            hits = [
                h
                for h in match_fields(text, context)
                if slot.kind == "field" or h.kind == slot.kind
            ]
            if len(hits) == 1:
                shape = _target_shape(card, slot)
                hit = hits[0]
                bound = column_table(hit.name, text, context) if hit.kind == "column" else hit.table
                if bound is None:
                    return None  # several tables have it: the question lists them
                return _field_value(slot.kind, bound, hit.name, shape)
        if slot.kind == "enum":
            if card.op in _RELATIONSHIP_OPS:
                stated = relationship_setting(slot.name, _marked(text, self.context))
                return stated if isinstance(stated, str) else None
            if card.op == "set_conditional_format" and slot.name == "style":
                found = _conditional_style(card, text)
                return found if isinstance(found, str) else None
            if card.op == "set_conditional_format" and slot.name in _ICON_PLACES:
                found = _icon_place(slot, text)
                return found if isinstance(found, str) else None
            if card.op == "set_conditional_format" and slot.name == "icon_set":
                kinds = _icon_kinds(card, text)
                return kinds[0] if len(kinds) == 1 else None
            if card.op == "add_navigator" and slot.name == "kind":
                navigator = re.search(r"\b(bookmark|page)\s+navigator\b", text, re.I)
                if navigator:
                    return navigator.group(1).lower()
            found = _choice_in(slot, text)
            return found if isinstance(found, str) else None
        if slot.kind == "bool":
            return _bool_value(slot, text, card)
        return None

    def _which_visual(
        self,
        card: ToolCard,
        values: Mapping[str, Any],
        slot: Slot,
        text: str,
        prompt: Prompt,
        evidence: Evidence | None = None,
    ) -> Clause | str:
        """The question for a step whose visual is not known: one ready choice per visual it
        could be (the ones a name that fits several could be, else the listed ones), each
        validated; nothing is applied until one is picked."""
        lists = prompt.lists
        wanted = set(evidence.candidates) if evidence is not None else set()
        pool = [e for e in lists.visuals if not wanted or e.id in wanted]
        pool = [
            e
            for e in pool
            if not e.hidden
            and (not visual_types_of(card) or e.visual_type in visual_types_of(card))
        ][:6]
        choices: list[Choice] = []
        for entry in pool:
            step = build_step(card, {**values, slot.name: entry.id}, self.context, source="model")
            if isinstance(step, PlanStep):
                label = f"{candidate_label(entry, self.context)}: {step.summary}"
                choices.append(Choice(label[:160], ops=(step.operation,), summary=step.summary))
        if not choices:
            return "I could not tell which visual you mean. Select it on the canvas first."
        return Clause(
            text,
            ParseKind.CLARIFY,
            question=f"Which visual should I change? ({card.label})",
            choices=tuple(choices),
        )

    def _which_table(
        self, card: ToolCard, values: Mapping[str, Any], missing: Sequence[Slot], text: str
    ) -> Clause | None:
        """The question for a column several tables have when the words name none of them
        ("hide the StoreKey column in the model": FactSales or DimStore?), one ready button
        per table, each validated; None when no such column is named."""
        column = next((s for s in card.slots if s.kind in {"column", "field"}), None)
        table_slot = card.slot("table")
        if column is None:
            return None
        hits = [h for h in match_fields(text, self.context) if h.kind == "column"]
        names = {h.name for h in hits}
        if len(names) != 1:
            return None
        name = names.pop()
        tables = tables_with_column(name, self.context)
        if len(tables) < 2:
            return None
        shape = _target_shape(card, column)
        choices: list[Choice] = []
        for table in tables:
            filled = {**values, column.name: _field_value(column.kind, table, name, shape)}
            if table_slot is not None and table_slot.kind == "table":
                filled[table_slot.name] = table
            step = build_step(card, filled, self.context, source="model")
            if isinstance(step, PlanStep):
                choices.append(
                    Choice(f"{table}[{name}]", ops=(step.operation,), summary=step.summary)
                )
        if not choices:
            return None
        listed = " and ".join(tables) if len(tables) == 2 else ", ".join(tables)
        return Clause(
            text,
            ParseKind.CLARIFY,
            question=f"{listed} each have a column {name}. Which one should I change?",
            choices=tuple(choices),
        )

    def _which_value(
        self,
        card: ToolCard,
        values: Mapping[str, Any],
        slot: Slot,
        text: str,
        prompt: Prompt,
        choices_said: Sequence[str] | None = None,
    ) -> Clause | str:
        """The question for a page or a choice the request does not give: one ready button
        per page (or per choice, or per choice the words leave open), each validated."""
        if slot.kind == "page":
            pages = prompt.lists.pages or self.context.pages
            if choices_said is not None:
                pages = [p for p in pages if p.id in choices_said]
            options = [(p.id, _page_label(p)) for p in pages]
        else:
            said = list(choices_said) if choices_said is not None else list(slot.choices)
            options = [(c, c.replace("_", " ")) for c in said]
        return _value_question(
            self.context,
            card,
            values,
            slot,
            text,
            options,
            source="model",
        )

    # -- formulas --------------------------------------------------------------------------------

    def _formula(
        self, card: ToolCard, slot: Slot, values: Mapping[str, Any], text: str
    ) -> tuple[str, list[str]] | str:
        """The checked formula for a ``dax``/``m`` slot: typed by the user, else drafted by the
        model (a failure is fed back once), else a question."""
        target = _target_name(card, values)
        requested: _ColumnBinaryRequest | None = None

        def check(expression: str, context: EditContext) -> list[str]:
            base = self.check_m if slot.kind == "m" else self.check_dax
            loop = _refers_to(expression, target) if slot.kind == "dax" and target else None
            role_problems = (
                validate_role_expression(expression, context)
                if (card.op in _ROLE_OPS and slot.kind == "dax")
                else []
            )
            bound = _column_binary_problem(expression, requested) if requested is not None else None
            return (
                ([loop] if loop else [])
                + base(expression, context)
                + role_problems
                + ([bound] if bound else [])
            )

        typed = typed_expression(text, slot.kind)
        if typed is not None:
            problems = [p for p in check(typed, self.context) if not _unverified(p)]
            if problems:
                return f"the formula you typed does not check out: {problems[0]}"
            return typed, self._formula_notes(typed, slot, drafted=False)
        if len(clean(m_prompt_text(text))) > MAX_REQUEST:
            return (
                "The calculation detail is too long for the local prompt. "
                "State this calculation and its fields in one short request."
            )
        if card.op == "create_calculated_column" and slot.kind == "dax":
            binary = _column_binary_request(text, values, self.context, target)
            if isinstance(binary, str):
                return binary
            requested = binary
        if slot.kind == "dax" and card.op in {"create_measure", "update_measure"}:
            patterned = self._business_formula(card, slot, values, text, check)
            if patterned is not None:
                return patterned
        if self.model is None or self.cancelled():
            return "type the formula, for example: = SUM(Table[Column])"
        error = ""
        for _ in range(2):
            if self._seconds_left() < 15:
                break
            self.progress("Asking the local AI to write the formula (up to a minute)")
            system, user = self.draft_prompt(card, slot, values, text, error)
            if requested is not None and any(
                _dax_column(requested.table, column) not in user
                for column in (requested.left, requested.right)
            ):
                return (
                    "There is not enough room for the calculation "
                    "and its explicitly requested fields. "
                    "Shorten this calculation request."
                )
            try:
                self.calls += 1
                answer = self.model.generate(
                    system,
                    user,
                    DraftAnswer,
                    max_output_tokens=DRAFT_TOKENS,
                    timeout_seconds=self._seconds_left(),
                    repair_attempts=0,
                )
            except Exception as failure:  # noqa: BLE001 - a failed draft becomes a question
                LOGGER.info("Drafting a formula failed: %s", str(failure)[:200])
                break
            expression = _clean_expression(answer.expression)
            problems = check(expression, self.context)
            blocking = [p for p in problems if not _unverified(p)]
            if not blocking:
                return expression, self._formula_notes(
                    expression, slot, drafted=True, unverified=bool(problems)
                )
            error = blocking[0]
        example = "= SUM(FactSales[SalesAmount])" if slot.kind == "dax" else "let ... in ..."
        detail = f" ({error})" if error else ""
        return (
            f"I could not write a formula that checks out{detail}. Type it in your message, "
            f"for example {example}"
        )

    def _business_formula(
        self,
        card: ToolCard,
        slot: Slot,
        values: Mapping[str, Any],
        text: str,
        check: Callable[[str, EditContext], list[str]],
    ) -> tuple[str, list[str]] | str | None:
        owner = self

        class SlotModel:
            def generate(
                self, system_prompt: str, user_prompt: str, response_model: Any, **kwargs: Any
            ) -> Any:
                assert owner.model is not None
                owner.calls += 1
                owner.prompt_bytes = max(
                    owner.prompt_bytes,
                    len(system_prompt.encode()) + len(user_prompt.encode()),
                )
                owner.progress("Choosing a business calculation and its fields locally")
                return owner.model.generate(system_prompt, user_prompt, response_model, **kwargs)

        built = None
        try:
            catalog = (
                self.pattern_catalog()
                if self.pattern_catalog is not None
                else dax_patterns.catalog_from_context(self.context)
            )
            built = dax_patterns.select_business_pattern(
                text,
                catalog,
                SlotModel()
                if self.pattern_catalog and self.model and not self.cancelled()
                else None,
                exclude_measure=_target_name(card, values),
                timeout_seconds=self._seconds_left(),
            )
            if built is None:
                return None
            if built.kind != "scalar":
                return "This pattern returns a table. Ask for it in DAX Lab instead of a measure."
            problems = [p for p in check(built.expression, self.context) if not _unverified(p)]
            if problems:
                return "The business pattern needs clarification: " + problems[0]
            notes = [
                "Studio built this formula from a business pattern and the model's named fields.",
                *built.notes,
                *self._formula_notes(built.expression, slot, drafted=False),
            ]
            if self.pattern_preview is not None:
                _, numbers = self.pattern_preview(built)
                notes.append(numbers)
            else:
                notes.append("Its numbers have not been evaluated on this data yet.")
            return built.expression, notes
        except dax_patterns.PatternError as error:
            return str(error)
        except Exception as error:  # noqa: BLE001 - formula remains reviewable or asks, never applies
            if built is not None and self.pattern_preview is not None:
                return f"Studio could not compute this business pattern: {error}"
            LOGGER.info("Business pattern selection failed: %s", str(error)[:200])
            return None

    def _formula_notes(
        self, expression: str, slot: Slot, *, drafted: bool, unverified: bool = False
    ) -> list[str]:
        notes: list[str] = []
        if drafted:
            notes.append(
                "The local AI wrote this formula; Studio checked its syntax and names, and Power "
                "BI Desktop checks it again when it is applied."
            )
        if unverified:
            notes.append("Studio could not check the Power Query code on this computer.")
        if slot.kind == "dax":
            said = describe_formula(expression, self.context)
            if said:
                notes.append(f"Studio reads the formula as: {said}.")
        return notes

    def draft_prompt(
        self, card: ToolCard, slot: Slot, values: Mapping[str, Any], text: str, error: str = ""
    ) -> tuple[str, str]:
        """The draft call's prompts: the request as typed, what the formula is for, a changed
        measure's current formula, the last check's own error, and only the names it may use
        (the most relevant first), within the budget. The model is the local one: the user's
        DAX goes to it and to nothing else (typed Power Query stays out, as everywhere)."""
        what = (
            "calculated column"
            if "column" in card.op
            else "role's row filter (a true/false test on one table's columns)"
            if card.op in _ROLE_OPS
            else "measure"
        )
        system = DRAFT_SYSTEM_M if slot.kind == "m" else DRAFT_SYSTEM.format(what=what)
        target = _target_name(card, values)
        head = [f"Request: {' '.join(clean(m_prompt_text(text)).split())[:MAX_REQUEST]}"]
        if target:
            label = "Role" if card.op in _ROLE_OPS else what.capitalize()
            head.append(f"{label}: {target}")
        if card.op == "update_measure" and target:
            # a change to a measure is written from what it is now (only its own formula)
            current = self.context.measure(target)
            if current is not None and current.expression.strip():
                now = " ".join(current.expression.split())[:600]
                head.append(f"Its formula now: {now}")
        if error:
            failed = " ".join(error.split())[:300]
            head.append(f"Your last formula failed: {failed} Write it again.")
        # the measure itself is left out: a measure that refers to itself is no formula
        ranked = [
            f
            for f in _rank_fields(text, self.context, None, [])
            if not (target and f[0] == "measure" and f[2].casefold() == target.casefold())
        ]
        for count in (40, 30, 20, 12, 6):
            measures = [f"[{n}]" for k, _, n in ranked[:count] if k == "measure"]
            columns = [_dax_column(t, n) for k, t, n in ranked[:count] if k == "column"]
            lines = [*head]
            if measures:
                lines.append("Measures: " + ", ".join(measures))
            if columns:
                lines.append("Columns: " + ", ".join(columns))
            user = "\n".join(lines)
            if len(system.encode("utf-8")) + len(user.encode("utf-8")) <= self.budget:
                return system, user
        return system, "\n".join(head)


def _dax_column(table: str, column: str) -> str:
    if not re.fullmatch(r"[A-Za-z_]\w*", table):
        table = "'" + table.replace("'", "''") + "'"
    return f"{table}[{column}]"


def _clean_expression(text: str) -> str:
    """The model's formula without a code fence or a leading "Name =" (it was asked not to
    write one; a VAR block keeps its own)."""
    expression = text.strip().strip("`").strip()
    expression = re.sub(r"^(?:dax|m)\s*\n", "", expression, flags=re.I)
    named = re.match(r"^\s*[\w %&/-]+\s*:?=\s*(?=\S)", expression)
    if named and not re.match(r"^\s*(?:VAR|let)\b", expression, re.I):
        expression = expression[named.end() :]
    return expression.strip()


def _target_name(card: ToolCard, values: Mapping[str, Any]) -> str:
    """The name of the measure (or column) a formula is for."""
    value = values.get("name") or values.get("new_name") or ""
    if isinstance(value, Mapping):
        value = value.get("name") or ""
    return str(value)


def _refers_to(expression: str, name: str) -> str | None:
    """A problem when a measure's formula uses the measure itself ("[Profit] - ..." for
    Profit): Power BI refuses it as a circular dependency."""
    # a measure is written [Name]; Table[Name] is a column of the same name, not the measure
    pattern = rf"(?<![\w'\]])\[\s*{re.escape(name.strip())}\s*\]"
    if re.search(pattern, expression, re.I):
        return f"The formula uses [{name}] itself, which Power BI refuses (a circular reference)."
    return None


# A change of the formula said in words: a change verb that governs "formula", "DAX",
# "expression" or "calculation" ("change the formula to...", "rewrite the DAX"), or a typed
# "=". Never after keep, leave or don't ("keep the formula as it is").
_FORMULA_CHANGE = re.compile(
    r"\b(?:change|update|rewrite|edit|fix|replace|set|make|write|modify|adjust|alter|correct|"
    r"redo|calculate)\b(?:\s+[\w'%-]+){0,4}?\s+(?:formula|dax|expression|calculation)s?\b|"
    r"\b(?:formula|dax|expression)\s+(?:to\b|should\s+(?:be|use|sum|count)|=)|"
    r"\bcalculation\s+(?:should\s+(?:be|use)|=)|"
    r"(?<![<>=!])=(?!=)",
    re.I,
)
_KEEP_FORMULA = re.compile(
    r"\b(?:keep|leave|preserve|retain|don'?t\s+(?:touch|change|edit)|do\s+not\s+(?:touch|change|"
    r"edit)|without\s+(?:touching|changing)|not)\b(?:\s+[\w'%-]+){0,3}?\s+(?:the\s+)?"
    r"(?:formula|dax|expression|calculation)",
    re.I,
)


_ROLE_OPS = frozenset({"create_role", "update_role"})
_SET_FILTER_OPS = frozenset({"set_visual_filter", "set_page_filter", "set_report_filter"})
# A role said by what its members see: "only sees the Online channel", "can see stores of
# type Store", "is limited to 2021", "has access to the East region".
_ROLE_SEES = re.compile(
    r"\b(?:sees?|seeing|can\s+(?:only\s+)?(?:see|view|access)|views?\s+only|only\s+"
    r"(?:views?|gets?|shows?|has|have)|(?:limited|restricted)\s+to|access\s+to|filter(?:s|ed)?"
    r"\s+(?:to|on|by))\b",
    re.I,
)


def _formula_tables(expression: str, context: EditContext) -> list[str]:
    """The tables whose columns a formula names ("DimChannel[ChannelName] = ..."; a bare
    "[ChannelName]" counts when one table has that column)."""
    found: list[str] = []
    for match in re.finditer(r"(?:'([^']+)'|([A-Za-z_][\w]*))?\s*\[([^\]]+)\]", expression):
        table = match.group(1) or match.group(2)
        column = match.group(3).strip()
        if table and table in context.tables:
            found.append(table)
        elif not table and context.measure(column) is None:
            owners = tables_with_column(column, context)
            if len(owners) == 1:
                found.append(owners[0])
    return list(dict.fromkeys(found))


def _without_name(text: str, name: str) -> str:
    """The request without a name it gives ("Total Sales Share" says no calculation)."""
    if not name.strip():
        return text
    return re.sub(re.escape(name.strip()), " ", text, flags=re.I)


def _wants_formula(text: str) -> bool:
    return _FORMULA_CHANGE.search(text) is not None and _KEEP_FORMULA.search(text) is None


def _negated(card: ToolCard, text: str) -> bool:
    """Whether the request says not to do what the card does: "don't sync the StoreName
    slicer" (a "don't" before the card's own words)."""
    own = _object_words(card) | {w for w in tokens(card.op.replace("_", " ")) if len(w) > 3}
    for match in _NEGATION.finditer(text):
        after = re.findall(r"[A-Za-z0-9']+", text[match.end() :])[:6]
        if {t for w in after for t in tokens(w)} & own:
            return True
    return False


def plan_request(
    pieces: Sequence[str],
    context: EditContext,
    model: PlannerModel | None,
    **options: Any,
) -> PlanOutcome:
    """``EditPlanner(context, model, **options).plan(pieces)``."""
    return EditPlanner(context, model, **options).plan(pieces)


__all__ = [
    "CHOOSE_TOKENS",
    "DRAFT_SYSTEM",
    "MAX_STEPS",
    "MODEL_SECONDS",
    "PLAN_SYSTEM",
    "PROMPT_BUDGET",
    "DraftAnswer",
    "EditPlanner",
    "PlanAnswer",
    "PlanOutcome",
    "PlanStep",
    "PlanStepAnswer",
    "Prompt",
    "Scored",
    "TooLarge",
    "answer_model",
    "available_cards",
    "build_prompt",
    "build_step",
    "card_clause",
    "check_dax",
    "check_m",
    "family_cards",
    "fill_from_words",
    "mask_names",
    "plan_request",
    "plan_schema",
    "read_step",
    "retrieve",
    "step_clause",
    "typed_expression",
    "validation_problem",
]
