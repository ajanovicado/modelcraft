"""The result types of the chat edit parser (re-exported by edit_intents)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ParseKind(StrEnum):
    APPLY = "apply"  # report-only edits, applied as one revision with an Undo
    CLARIFY = "clarify"  # ask first; nothing is applied without a choice
    UNSUPPORTED = "unsupported"  # out of scope: a reason and the steps in Power BI Desktop
    UNDO = "undo"
    REDO = "redo"
    RESTORE = "restore"
    WHAT_CHANGED = "what_changed"
    MODEL_EDIT = "model_edit"  # a measure change: what-if first, never applied automatically
    NOT_AN_EDIT = "not_an_edit"  # a question or remark: answered normally, nothing changes


@dataclass(frozen=True)
class Target:
    visual_ids: tuple[str, ...]
    page_id: str | None
    label: str
    # how the target was resolved: selection, caption, field, position, type, hue, page, all,
    # carry (the clause before), forced (a chosen candidate) or measure
    how: str


@dataclass(frozen=True)
class Choice:
    """One button of a CLARIFY: it either carries ready operations or a slot fill."""

    label: str
    ops: tuple[dict[str, Any], ...] = ()
    fill: dict[str, Any] = field(default_factory=dict)
    summary: str = ""  # the receipt line when the choice carries operations


@dataclass(frozen=True)
class WhatIf:
    question: str  # "What if [Sales Target] used 120000 instead of 100000?"
    measure: str
    new_expression: str


@dataclass(frozen=True)
class Clause:
    """One clause of the user's message and what to do with it."""

    text: str
    kind: ParseKind
    # operations exactly as report_edits validates them (op discriminator plus fields)
    ops: tuple[dict[str, Any], ...] = ()
    # the plain-English receipt line; it names the resolved target
    summary: str = ""
    targets: tuple[Target, ...] = ()
    question: str = ""  # CLARIFY
    choices: tuple[Choice, ...] = ()  # CLARIFY
    capability: str = ""  # UNSUPPORTED: a key of CAPABILITY_MATRIX
    reason: str = ""  # UNSUPPORTED
    desktop_steps: tuple[str, ...] = ()  # UNSUPPORTED
    what_if: WhatIf | None = None  # MODEL_EDIT preview request

    @property
    def needs_confirmation(self) -> bool:
        """A model edit is never applied automatically (it needs Power BI and a click)."""
        return self.kind is ParseKind.MODEL_EDIT and bool(self.ops)


@dataclass(frozen=True)
class ParseResult:
    clauses: tuple[Clause, ...]
    remainder: str  # text that is not an edit (a question to answer normally)
    # Edit-looking clauses the deterministic tier could not read. The service gives these
    # to the bounded 4B fallback (model_index / ModelIntent); none when everything was read.
    unparsed: tuple[str, ...] = ()

    @property
    def ops(self) -> list[dict[str, Any]]:
        """Every APPLY clause's operations, in order, for ONE revision."""
        return [op for c in self.clauses if c.kind is ParseKind.APPLY for op in c.ops]

    @property
    def receipts(self) -> list[str]:
        """One line per clause that changed, asked or refused something."""
        lines: list[str] = []
        for clause in self.clauses:
            if clause.kind is ParseKind.CLARIFY:
                lines.append(clause.question)
            elif clause.kind is ParseKind.UNSUPPORTED:
                lines.append(clause.reason)
            elif clause.summary:
                lines.append(clause.summary)
        return lines


@dataclass(frozen=True)
class Mention:
    """A caption or measure name that still says an old number after a measure changed."""

    kind: str  # "caption" | "measure"
    visual_id: str | None
    text: str  # what it says now
    suggestion: str  # what it would say with the new number ("" when none was given)
    ops: tuple[dict[str, Any], ...] = ()  # rename_measure; captions are advisory only
