"""The working state of one clause while it is being read, and how it answers.

A ``Frame`` holds the clause text, the target the resolver found and the choices made
earlier (``Forced``); its methods build the answers every recognizer shares: an APPLY
clause that names its targets, a CLARIFY that lists candidates, an UNSUPPORTED clause that
cites the capability matrix.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .edit_capabilities import CAPABILITY_MATRIX, STYLING_KEYS, Capability
from .edit_context import EditContext, VisualEntry, type_name
from .edit_ops import Op, Unfit
from .edit_targets import Resolution, ambiguity_question, candidate_labels
from .edit_types import Choice, Clause, ParseKind, Target

NOT_AVAILABLE = "not_available_on_visual"


@dataclass
class Forced:
    """Answers already given to a CLARIFY, applied when its clause is read again."""

    visuals: list[str] | None = None
    append: str = ""
    old: float | None = None
    measure: str | None = None

    @classmethod
    def from_fill(cls, fill: dict[str, Any]) -> Forced:
        visuals = fill.get("visual")
        if isinstance(visuals, str):
            visuals = [v for v in visuals.split(",") if v]
        return cls(
            visuals=list(visuals) if visuals else None,
            append=str(fill.get("append", "")),
            old=fill.get("old"),
            measure=fill.get("measure"),
        )


def names_phrase(entries: list[VisualEntry]) -> str:
    """The visuals a receipt names: one, up to three, or "4 charts ('A', 'B', 'C' and 1 more)"."""
    labels = [e.label for e in entries]
    if len(labels) == 1:
        return labels[0]
    if len(labels) <= 3:
        return ", ".join(labels[:-1]) + " and " + labels[-1]
    kinds = {type_name(e.visual_type) for e in entries}
    noun = next(iter(kinds)) if len(kinds) == 1 else "visual"
    shown = ", ".join(labels[:3])
    return f"{len(labels)} {noun}s ({shown} and {len(labels) - 3} more)"


@dataclass
class Frame:
    context: EditContext
    text: str  # the clause as written
    low: str  # casefolded
    masked: str  # casefolded, with the words that named the target blanked out
    res: Resolution
    forced: Forced = field(default_factory=Forced)
    carry: list[VisualEntry] = field(default_factory=list)
    # Whether a visual has what the clause changes; set by the recognizer that knows.
    fit: Callable[[VisualEntry], bool] | None = None

    # --- answers ---------------------------------------------------------------------------

    def target(self, entries: list[VisualEntry]) -> Target:
        pages = {e.page_id for e in entries}
        return Target(
            visual_ids=tuple(e.id for e in entries),
            page_id=next(iter(pages)) if len(pages) == 1 else None,
            label=names_phrase(entries),
            how=self.res.how or "selection",
        )

    def apply(
        self, entries: list[VisualEntry], ops: list[Op], summary: str, *, how: str | None = None
    ) -> Clause:
        target = self.target(entries)
        if how:
            target = Target(target.visual_ids, target.page_id, target.label, how)
        return Clause(
            text=self.text,
            kind=ParseKind.APPLY,
            ops=tuple(ops),
            summary=summary,
            targets=(target,),
        )

    def unsupported(self, capability: Capability | str, reason: str = "") -> Clause:
        steps: tuple[str, ...]
        if isinstance(capability, str):
            key, steps = capability, ()
            title = capability
        else:
            key, steps, title = capability.key, capability.desktop_steps, capability.title
            reason = reason or capability.reason
        text = reason or f"Studio cannot do {title.lower()} yet."
        return Clause(
            text=self.text,
            kind=ParseKind.UNSUPPORTED,
            capability=key,
            reason=text,
            desktop_steps=steps,
            targets=(self.target(self.res.entries),) if key in STYLING_KEYS and self.res.ok else (),
        )

    def not_available(self, reason: str) -> Clause:
        return Clause(
            text=self.text,
            kind=ParseKind.UNSUPPORTED,
            capability=NOT_AVAILABLE,
            reason=reason,
        )

    def clarify(
        self,
        question: str,
        choices: list[Choice] | None = None,
        targets: list[VisualEntry] | None = None,
    ) -> Clause:
        return Clause(
            text=self.text,
            kind=ParseKind.CLARIFY,
            question=question,
            choices=tuple(choices or ()),
            targets=(self.target(targets),) if targets else (),
        )

    def ask_target(self) -> Clause:
        """A CLARIFY for a target the request did not pin down: the candidates, or how to
        name one."""
        res = self.res
        labels = candidate_labels(res.candidates[:6], self.context)
        choices = [
            Choice(label=label, fill={"visual": c.id})
            for label, c in zip(labels, res.candidates[:6], strict=True)
        ]
        if 1 < len(res.candidates) <= 8:
            ids = ",".join(c.id for c in res.candidates)
            choices.insert(
                0,
                Choice(label=f"All {len(res.candidates)}", fill={"visual": ids}),
            )
        question = res.question or "Which visual? Select it on the canvas or name it."
        return self.clarify(question, choices)

    def for_each(
        self,
        entries: list[VisualEntry],
        build: Callable[[VisualEntry], list[Op]],
        summary: Callable[[list[VisualEntry]], str],
    ) -> Clause:
        """Build operations per visual; visuals that cannot take the change are skipped and
        named in the receipt; when none can, the reason is the answer."""
        ops: list[Op] = []
        fitted: list[VisualEntry] = []
        skipped: list[tuple[VisualEntry, str]] = []
        for entry in entries:
            try:
                made = build(entry)
            except Unfit as error:
                skipped.append((entry, str(error)))
                continue
            ops += made
            fitted.append(entry)
        if not fitted:
            if len(skipped) == 1:
                return self.not_available(skipped[0][1])
            reasons = "; ".join(dict.fromkeys(reason for _, reason in skipped[:3]))
            return self.not_available(f"None of those visuals can take that change: {reasons}.")
        line = summary(fitted)
        if skipped:
            names = names_phrase([e for e, _ in skipped])
            line += f" Skipped {names}: {skipped[0][1].rstrip('.')}."
        return self.apply(fitted, ops, line)


def need_entries(f: Frame) -> list[VisualEntry] | Clause:
    """The resolved visuals, or the CLARIFY that asks for them.

    When the request named no target at all, only the visuals that have what it changes are
    candidates: "add data labels" can only mean a chart. A single such visual is the target.
    """
    res = f.res
    if res.ok:
        return res.entries
    if not res.explicit and not res.entries and f.fit is not None:
        fit = f.fit
        offered = [v for v in res.pool if fit(v)]
        if len(offered) == 1:
            res.entries, res.how, res.candidates = offered, "fit", []
            return offered
        if len(offered) > 1:
            res.candidates = offered
            res.question = ambiguity_question(offered, f.context, "visual")
    return f.ask_target()


def capability(key: str) -> Capability:
    return CAPABILITY_MATRIX[key]
