"""The shared contract between edit operations, the local model's planner and Desktop probes.

Every operation family describes its operations as ``ToolCard``s. The planner (the 4B model's
route) never writes an operation itself: it picks a card and fills the card's slots, each a small
typed value (a visual, a page, a field, a color, a number, a word from a list), and the card turns
the filled slots into the operation dict that ``report_edits.parse_operations`` validates. Cards are
also what the capability matrix, the eval cases and the Format pane's help text are built from.

Every report-layer family also lists ``ProbeCase``s: small batches applied to a scratch copy of a
real dashboard and opened in Power BI Desktop (``evals/imported_edit/probe_ops.py``), so a new
operation is only called done once Desktop has shown it and kept it through a save.

A family exports ``TOOL_CARDS: tuple[ToolCard, ...]`` and (report layer) ``DESKTOP_PROBES:
tuple[ProbeCase, ...]`` from its module; ``FAMILY_MODULES`` lists the modules the registry reads.
"""

from __future__ import annotations

import copy
import importlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

LOGGER = logging.getLogger(__name__)

SlotKind = Literal[
    "visual",  # a visual on the report (resolved from captions, selection, position words)
    "visuals",  # several visuals
    "page",
    "field",  # a column or measure reference {table?, name}
    "fields",
    "measure",
    "column",
    "table",
    "color",  # hex or a theme color
    "number",
    "integer",
    "text",
    "enum",  # one of ``choices``
    "bool",
    "dax",  # a DAX expression (drafted, then checked)
    "m",  # a Power Query M expression (drafted, then checked)
]


@dataclass(frozen=True)
class Slot:
    """One value the planner fills. ``field`` is the operation field it lands in (dotted for a
    nested one, e.g. ``filter.values``); ``choices`` is required for ``enum``.

    What a filled slot holds when ``ToolCard.to_operation`` (or ``build``) receives it: a visual
    id (``visual``), a list of ids (``visuals``), a page id (``page``), ``{"table", "name"}``
    (``field``, ``column``; a list of them for ``fields``), a measure name (``measure``; the
    ``{"table", "name"}`` pair when the operation field it lands in takes a field reference),
    a table name (``table``), ``"#RRGGBB"`` (``color``), a number, a bool, a short text copied
    from the request, one of ``choices`` (``enum``) or an expression that passed its checks
    (``dax``, ``m``).

    ``words`` are other words a request uses for what the slot sets ("frame", "outline" for a
    ``border`` bool): the planner fills a bool, or accepts the model's value for it, only when the
    request says the slot's name or one of these."""

    name: str
    kind: SlotKind
    description: str
    required: bool = True
    field: str | None = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    words: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCard:
    """What the planner (and people reading the capability list) know about one operation.

    ``summary`` is one plain line; ``cues`` are words and short phrases a request for it tends to
    contain (retrieval uses them, so a card is found without a model call); ``examples`` pair a
    request with the operation it should become (they double as eval cases). ``fixed`` holds
    operation fields the card always sets. ``build`` turns filled slots into the operation dict
    when a straight slot-to-field mapping is not enough.

    ``capability`` names the ``edit_capabilities.CAPABILITY_MATRIX`` entry the card provides (a
    family that leaves it empty is matched to a planned entry by its examples);
    ``visual_types`` are the visual types its ``visual`` slot fits (empty: any);
    ``planner=False`` keeps a card out of what the local model may choose (it is still listed).
    ``title`` is the short line a plan card shows for a step ("Change a visual's title"; for a
    card that adds or removes by a mode, the planner words the mode itself); empty: the
    summary's first clause.
    """

    op: str
    family: str
    summary: str
    slots: tuple[Slot, ...]
    cues: tuple[str, ...] = ()
    examples: tuple[tuple[str, Mapping[str, Any]], ...] = ()
    fixed: Mapping[str, Any] = field(default_factory=dict)
    needs_desktop: bool = False  # model changes: applied through Power BI Desktop
    confirm: bool = False  # never applied without the user's explicit yes
    build: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None
    capability: str = ""
    visual_types: tuple[str, ...] = ()
    planner: bool = True
    title: str = ""

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)

    @property
    def label(self) -> str:
        """``title``, else the first clause of ``summary`` ("Change a visual's title")."""
        if self.title:
            return self.title
        first = re.split(r"[:;.]|,\s|\s\(", self.summary, maxsplit=1)[0].strip()
        return first or self.op.replace("_", " ")

    def to_operation(self, filled: Mapping[str, Any]) -> dict[str, Any]:
        """The operation dict for these slot values (validation happens in report_edits)."""
        if self.build is not None:
            return dict(self.build(filled))
        operation: dict[str, Any] = {"op": self.op, **copy.deepcopy(dict(self.fixed))}
        for slot in self.slots:
            if slot.name not in filled:
                continue
            target = operation
            path = (slot.field or slot.name).split(".")
            for part in path[:-1]:
                target = target.setdefault(part, {})
            target[path[-1]] = filled[slot.name]
        return operation


@dataclass(frozen=True)
class ProbeCase:
    """A batch to try in real Power BI Desktop on a scratch copy of the Agents dashboard.

    ``operations`` are op dicts as chat sends them (visual ids and page names of that dashboard);
    ``expect`` says, in words, what a person should see on the screenshot; ``check`` (optional)
    inspects the parsed JSON of every part of the file Desktop saved and returns problems ([] ok).
    """

    name: str
    family: str
    operations: tuple[Mapping[str, Any], ...]
    expect: str
    page: str = "1"
    check: Callable[[Mapping[str, Any]], list[str]] | None = None


# Modules that export TOOL_CARDS and/or DESKTOP_PROBES. A module that does not exist yet, or does
# not define them, is skipped, so families can land one at a time.
FAMILY_MODULES: tuple[str, ...] = (
    "edit_tool_cards",  # cards for the operations that existed before the planner
    "report_edit_conditional",
    "report_edit_navigation",
    "report_edit_extras",
    "report_edit_model",
)


def _load(module: str) -> Any:
    try:
        return importlib.import_module(f"{__package__}.{module}")
    except ModuleNotFoundError as error:
        if error.name and error.name.endswith(module):
            return None
        raise


def tool_cards(modules: Sequence[str] = FAMILY_MODULES) -> tuple[ToolCard, ...]:
    """Every card the families define, in module order; an op may appear once only."""
    found: list[ToolCard] = []
    seen: set[str] = set()
    for name in modules:
        module = _load(name)
        for card in getattr(module, "TOOL_CARDS", ()) if module is not None else ():
            if card.op in seen:
                raise ValueError(f"Two tool cards describe the operation {card.op!r}")
            seen.add(card.op)
            found.append(card)
    return tuple(found)


def desktop_probes(modules: Sequence[str] = FAMILY_MODULES) -> tuple[ProbeCase, ...]:
    found: list[ProbeCase] = []
    for name in modules:
        module = _load(name)
        found.extend(getattr(module, "DESKTOP_PROBES", ()) if module is not None else ())
    return tuple(found)


# The module with the cards of the operations that existed before the planner: the hand-built
# parser (edit_parser) reads requests for those, so the cards of this module never take a
# request away from it.
BUILTIN_MODULE = "edit_tool_cards"


def registered_operations() -> dict[str, type[Any]]:
    """The operation classes ``report_edits.parse_operations`` accepts, by op name. A family
    module can define cards before its operations are registered there; such a card is listed,
    but nothing can apply its operation yet."""
    from . import report_edits  # imported here: the family modules import this module

    return {str(cls.model_fields["op"].default): cls for cls in report_edits.REPORT_OPERATIONS}


def operation_classes(modules: Sequence[str] = FAMILY_MODULES) -> dict[str, type[Any]]:
    """Every operation class by op name: the registered ones, and those a family module defines
    in a tuple of operation classes (its ``*_OPERATIONS``) before it is registered, so a card's
    slots can be matched to its operation's fields either way."""
    found = dict(registered_operations())
    for name in modules:
        try:
            module = _load(name)
        except Exception:  # noqa: BLE001 - a family that does not import has no classes to read
            LOGGER.warning("The tool family %s could not be imported", name, exc_info=True)
            continue
        for value in vars(module).values() if module is not None else ():
            if not isinstance(value, tuple) or not value or not all(map(_is_operation, value)):
                continue
            for cls in value:
                found.setdefault(str(cls.model_fields["op"].default), cls)
    return found


def _is_operation(value: object) -> bool:
    return isinstance(value, type) and "op" in getattr(value, "model_fields", {})


def planner_cards(modules: Sequence[str] = FAMILY_MODULES) -> tuple[ToolCard, ...]:
    """The cards the planner may offer: their operation is registered and they did not opt
    out (``planner=False``)."""
    known = registered_operations()
    return tuple(card for card in tool_cards(modules) if card.op in known and card.planner)


def builtin_ops() -> frozenset[str]:
    """The operations whose cards live in ``BUILTIN_MODULE``."""
    return frozenset(card.op for card in tool_cards((BUILTIN_MODULE,)))
