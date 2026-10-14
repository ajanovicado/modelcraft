"""Chat editing of an imported Power BI report: a message in, one revision and a receipt out.

``imported_edit_events`` reads a message with ``edit_intents.parse_edit_message`` (no model),
does what each clause asks through the ``EditPort`` of ``imported_editing`` and streams the
SSE protocol of docs/imported-report-editing-api.md ("Chat"): ``edit_progress``,
``edit_applied``, ``edit_choices``, ``edit_confirm``, ``edit_failed`` and the ``token`` text of
a short plain-English reply. The rules it keeps (design review P8, P21, P30, P31, P32, P33):

- One message is one revision: every APPLY clause goes into ONE batch. Clauses are handled in
  message order, so an undo runs before the edits that follow it (which are then read again
  against the report the undo left). Something a clause needs to ask (a CLARIFY, a measure
  change, a confirmation the edit layer wants) never blocks the clauses that were clear.
- A measure change is never applied automatically (P32): the local what-if comes first, then
  ``edit_confirm``; ``POST .../chat/edit-confirm`` is the only way it is applied.
- The batch is not tied to the client (P8). The whole turn runs on its own thread; the
  generator returned to the API only relays events. If the client goes away the batch still
  finishes (it runs on the report's serial worker), and the outcome is written to the chat
  history by that thread under the request's idempotency key, so a reload shows it and a
  retry with the same key replays it instead of applying twice.
- The 4B model is the last resort (P30) and only for text the deterministic tier could not
  read: the planner (edit_planner) lets it choose among a few tool cards and fill their slots,
  one choose call of 60 s at most (a formula it has to write is one more short call); any
  unusable plan gives suggestion chips; a runtime failure says how to retry. Neither applies
  a change. What it planned is
  shown as ``edit_plan`` and applied only on Apply (``POST .../chat/edit-confirm``), as one
  revision through the same port as any edit.
- Undo only reverts the newest change (P21); "restore" asks first and lists what it leaves out.

Nothing here calls the model when the deterministic tier read the message, and the module has
no idea how Power BI Desktop is driven: every Desktop question arrives as an ``ImportedEditError``.
"""

from __future__ import annotations

import logging
import queue
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextvars import copy_context
from dataclasses import dataclass, replace
from datetime import datetime
from functools import cache
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from pydantic import BaseModel

from . import report_edit_model, report_edits
from .chat_context import desktop_steps
from .contracts import ChatEditChoiceRequest, ChatEditConfirmRequest, ChatRequest
from .dax_patterns import BuiltPattern, PatternCatalog
from .edit_capabilities import STYLING_KEYS
from .edit_context import EditContext, VisualEntry, hue_family, type_name
from .edit_frame import NOT_AVAILABLE, Forced
from .edit_intents import (
    apply_choice,
    parse_edit_message,
    stale_literal_mentions,
)
from .edit_parser import Parser, numeric_literals
from .edit_planner import EditPlanner, PlanOutcome, PlanStep, card_clause
from .edit_text import short_number, tokens
from .edit_types import Choice, Clause, ParseKind, ParseResult
from .imported_editing import EditPort, ImportedEditError
from .report_editor import TESTS_CONFIRMATION
from .report_revisions import ORIGINAL_ID

if TYPE_CHECKING:
    from .contracts import DatasetSnapshot
    from .report_import import ImportedReport

LOGGER = logging.getLogger(__name__)

Event = dict[str, Any]

MAX_OPERATIONS = 50  # the EditBatch limit: one message is one revision, so more is refused
PENDING_TTL_SECONDS = 30 * 60
PENDING_LIMIT = 100
MODEL_SECONDS = 60  # one call of the 4B tier (edit_planner.MODEL_SECONDS)
MAX_CHIPS = 6
HISTORY_LINES = 6
GENERIC_FAILURE = "Studio could not finish this change. Check the report and try again."
EXPIRED = "That question has expired. Ask again and I will look at the report as it is now."
# The service unloads the 4B model only when it opens a working copy in Desktop for the user
# (``submit_open`` calls ``before_launch``); the Desktop cycle of a confirmed measure change does
# not, so this text must not promise an unload (it was untrue once, P33): it only says what is
# certain, that Desktop is needed and how long it takes.
MODEL_EDIT_NOTE = (
    "Applying changes the measure in the report itself and needs Power BI Desktop (about 1 minute)."
)
_GENERIC_MODEL_LINE = "Change the model."  # imported_editing's line for a non-measure change
# What a model change changes, by a word of its operation's name (for the note above when the
# change is not a measure's).
_MODEL_THINGS = (
    ("calculation_item", "calculation items"),
    ("calculation_group", "calculation groups"),
    ("field_parameter", "field parameters"),
    ("relationship", "relationships"),
    ("hierarchy", "hierarchies"),
    ("role", "security roles"),
    ("named_expression", "queries"),
    ("query", "queries"),
    ("refresh_data", "the data (a refresh)"),
    ("model_options", "the model's settings"),
    ("date_table", "the date table"),
    ("column", "columns"),
    ("table", "tables"),
    ("measure", "measures"),
)


def model_edit_note(ops: Sequence[Mapping[str, Any]]) -> str:
    """What applying these model changes does, worded from what they change: a measure's
    change keeps ``MODEL_EDIT_NOTE``; a column, a relationship or a role says so."""
    things: list[str] = []
    for op in ops:
        name = str(op.get("op", ""))
        found = next((said for key, said in _MODEL_THINGS if key in name), None)
        if found and found not in things:
            things.append(found)
    if not things or things == ["measures"]:
        return MODEL_EDIT_NOTE
    listed = ", ".join(things[:-1]) + (" and " if len(things) > 1 else "") + things[-1]
    return (
        f"Applying changes the report's data model ({listed}) and needs Power BI Desktop "
        "(about 1 minute)."
    )


_LEFT_AS_IT_WAS = "Okay, I left the report as it was."
_SKIPPED = (
    "I did not start the other changes in your message because the step before them did not "
    "finish. Ask for them again afterwards."
)
_UNSAVED_DETAIL = (
    "Power BI Desktop has unsaved changes to this report. If you continue, Studio first saves "
    "that work as its own step in the history (nothing is lost), then makes this change."
)
_ASKS = re.compile(
    r"\?|^\s*(?:what|which|who|how|why|when|where|is|are|do|does|can|could"
    r"|(?:tell|show|give)\s+me)\b",
    re.I,
)
_WHAT_IF_FOOTER = re.compile(r"\n\s*\n\s*The report has not changed\..*\Z", re.S)
_UNDID = re.compile(r"^Undid r\d+:\s*")
_RESTORED = re.compile(r"^Restored to r\d+:\s*")
_AUTHORS = {
    "studio": "made in Studio",
    "desktop": "saved in Power BI Desktop",
    "undo": "an undo",
    "restore": "a restore",
}
# The flag an op takes as the answer to a NeedsConfirmation kind, when it is not the kind's name.
_CONFIRM_FIELD = {
    "remove_page_references": "cleanup_references",
    "remove_bookmark_references": "cleanup_references",
    "clear_page_links": "cleanup_references",
}
_STATUS = {
    "expired": 410,
    "head_moved": 409,
    "desktop_unsaved": 409,
    "desktop_dialog": 409,
    "desktop_busy": 409,
    "edit_busy": 409,
    "conflict": 409,
    "idempotency_conflict": 409,
    "desktop_unavailable": 503,
    "edit_failed": 500,
}
# Common edits offered as chips when the message could not be read: (label, sentence).
_CHIP_EDITS = (
    ("Show {t} in thousands", "set the number format to thousands"),
    ("Duplicate {t}", "duplicate it"),
    ("Move {t} left", "move it left"),
    ("Hide {t}", "hide it"),
    ("Make {t} bigger", "make it bigger"),
)


def _event(name: str, data: dict[str, Any]) -> Event:
    return {"event": name, "data": data}


def _token(text: str) -> Event:
    return _event("token", {"text": text})


# --- what the chat asks of the service -------------------------------------------------------


_Answer = TypeVar("_Answer", bound=BaseModel)


class IntentModel(Protocol):
    """The guarded local model, as much of it as the 4B tier (the planner) uses."""

    def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: type[_Answer],
        *,
        max_output_tokens: int | None = None,
        timeout_seconds: int | None = None,
        repair_attempts: int | None = None,
    ) -> _Answer: ...


@dataclass(frozen=True)
class Question:
    """What is left of a message once its edits are done: a question for the answer paths."""

    text: str
    edited: bool  # an edit was read from the same message (its receipt is already shown)
    presentation: bool  # "make a table of ..." asked for figures, not a new visual
    turn: bool  # nothing of this message is in the history yet: the answer records the turn
    allow_model: bool = True  # False when a what-if already answered (no model for the rest)


@dataclass(frozen=True)
class Action:
    """What a confirmed (or chosen) answer does: one port call."""

    verb: Literal["apply", "undo", "restore"] = "apply"
    ops: tuple[dict[str, Any], ...] = ()
    summary: str = ""
    revision: str | None = None  # restore: the revision to go back to
    text: str = ""  # the words that asked for it (for the Desktop steps of a refusal)
    confirm_model: bool = False
    confirmed_discard: bool = False
    desktop_work: Literal["ask", "save"] = "ask"
    # NeedsConfirmation kinds the user accepted, each with the step (1-based) that asked
    confirm_flags: tuple[tuple[str, int | None], ...] = ()
    what_if: dict[str, str] | None = None
    literal: tuple[float, float] | None = None  # (old, new) number a measure edit changed


@dataclass(frozen=True)
class Option:
    label: str
    action: Action | None  # None: "leave it as it is"


@dataclass
class Pending:
    """A question the user has not answered yet."""

    workspace_id: str
    kind: Literal["clarify", "options", "confirm"]
    head: str | None  # the head the question was read against
    selection: str | None = None
    page: str | None = None
    clause: Clause | None = None  # clarify
    options: tuple[Option, ...] = ()  # options
    action: Action | None = None  # confirm
    id: str = ""
    created: float = 0.0


class Flight:
    """The events of an answer that is being carried out, kept so that a retry with the same
    idempotency key can follow it from the start instead of starting a second turn."""

    def __init__(self, signature: tuple[object, ...]) -> None:
        self.signature = signature
        self._events: list[Event] = []
        self._done = False
        self._changed = threading.Condition()

    def add(self, item: Event) -> None:
        with self._changed:
            self._events.append(item)
            self._changed.notify_all()

    def finish(self) -> None:
        with self._changed:
            self._done = True
            self._changed.notify_all()

    def follow(self, cancel: threading.Event) -> Iterator[Event]:
        index = 0
        while True:
            item: Event | None = None
            with self._changed:
                if index >= len(self._events) and not self._done:
                    self._changed.wait(0.2)
                if index < len(self._events):
                    item = self._events[index]
                    index += 1
                elif self._done:
                    return
            if item is None:
                if cancel.is_set():
                    return
                continue
            yield item


class PendingEdits:
    """Open questions, in memory, bounded and expiring (30 minutes): a restart forgets them,
    and the answer then says the question has expired."""

    def __init__(
        self,
        *,
        ttl: float = PENDING_TTL_SECONDS,
        limit: int = PENDING_LIMIT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._limit = limit
        self._clock = clock
        self._items: OrderedDict[str, Pending] = OrderedDict()
        self._flights: dict[tuple[str, str], Flight] = {}
        self._latest: dict[str, str] = {}  # workspace -> the question asked last in its chat
        self._lock = threading.Lock()

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._items.items() if now - p.created > self._ttl]:
            del self._items[key]
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def add(self, pending: Pending) -> str:
        with self._lock:
            pending.id = secrets.token_urlsafe(9)
            pending.created = self._clock()
            self._items[pending.id] = pending
            self._latest[pending.workspace_id] = pending.id
            self._purge()
            return pending.id

    def latest(self, workspace_id: str) -> Pending | None:
        """The question asked last in this workspace's chat, while it is open and nothing was
        said after it: what a typed "yes" or "no" answers."""
        with self._lock:
            self._purge()
            found = self._latest.get(workspace_id)
            return self._items.get(found) if found else None

    def forget_latest(self, workspace_id: str) -> None:
        """A new message was said: a typed "yes" no longer answers the question before it (its
        buttons still do)."""
        with self._lock:
            self._latest.pop(workspace_id, None)

    def take(self, workspace_id: str, pending_id: str) -> Pending | None:
        """The open question, removed (each is answered once); None when it is unknown, of
        another workspace or too old."""
        with self._lock:
            self._purge()
            found = self._items.get(pending_id)
            if found is None or found.workspace_id != workspace_id:
                return None
            del self._items[pending_id]
            return found

    def put_back(self, pending: Pending) -> None:
        """Keep a question open after an answer that could not be carried out (a busy report)."""
        with self._lock:
            self._items[pending.id] = pending
            self._purge()

    def begin(
        self, workspace_id: str, key: str, signature: tuple[object, ...]
    ) -> tuple[Flight, bool]:
        """The answer running under an idempotency key, and whether the caller started it. A
        second request with the same key and the same answer gets the first one's flight."""
        with self._lock:
            found = self._flights.get((workspace_id, key))
            if found is not None and found.signature == signature:
                return found, False
            flight = Flight(signature)
            if found is None:
                self._flights[(workspace_id, key)] = flight
            return flight, True

    def end(self, workspace_id: str, key: str, flight: Flight) -> None:
        with self._lock:
            if self._flights.get((workspace_id, key)) is flight:
                del self._flights[(workspace_id, key)]

    def __len__(self) -> int:
        with self._lock:
            self._purge()
            return len(self._items)


def _ignore(_kind: str, _payload: dict[str, Any]) -> None:
    return None


@dataclass
class ChatEditEnv:
    """The parts of the chat service the edit flow calls; each is a plain callable so the flow
    is tested without the service."""

    pending: PendingEdits
    # (request, workspace id, the user's words, {"text", "draft_id", "edit_events"}): the turn
    # in the chat history, once per idempotency key
    record: Callable[[object, str, str, dict[str, Any]], None]
    what_if: Callable[[str], str | None]  # the local what-if answer for a request, no model
    answer: Callable[[Question, threading.Event], Iterator[Event]]  # the question paths
    model: IntentModel | None = None
    log: Callable[[str, dict[str, Any]], None] = _ignore
    pattern_catalog: Callable[[], PatternCatalog] | None = None
    pattern_preview: Callable[[BuiltPattern], tuple[str, str]] | None = None


# --- text ---------------------------------------------------------------------------------------


def _plain(summary: str) -> str:
    """A history summary without the internal revision ids of an undo or a restore."""
    text = _UNDID.sub("Went back one change: ", summary.strip())
    return _RESTORED.sub("Restored an earlier version: ", text)


def _when(created: object) -> str:
    try:
        stamp = datetime.fromisoformat(str(created)).astimezone()
    except ValueError:
        return ""
    return f"{stamp.day} {stamp:%b %H:%M}"


def _history_line(row: dict[str, Any], *, dated: bool = False) -> str:
    """One history row in plain words: what it did, who made it and (optionally) when."""
    who = _AUTHORS.get(str(row.get("author")), "made in Studio")
    when = _when(row.get("created")) if dated else ""
    return f"{_plain(str(row.get('summary', '')))} ({who}{', ' + when if when else ''})"


def _bullets(items: Sequence[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _capped(items: Sequence[str], limit: int) -> list[str]:
    shown = list(items[:limit])
    if len(items) > limit:
        shown.append(f"and {len(items) - limit} more")
    return shown


def _unsupported_text(clause: Clause, steps: Sequence[str]) -> str:
    reason = clause.reason or "Studio cannot make that change."
    if not steps or clause.capability in STYLING_KEYS:
        return reason
    return (
        f"{reason} To do it in Power BI Desktop, use Open in Power BI (it opens a working "
        f"copy; your original file stays as it is):\n{_bullets(steps[:4])}"
    )


def _problem_text(error: ImportedEditError, action: Action, steps: Sequence[str]) -> str:
    code, extra = error.code, error.extra
    if code == "head_moved":
        return (
            "The report changed while I was working on this (another change was saved "
            "first), so I did not apply it. Ask again and I will use the latest version."
        )
    if code == "desktop_dialog":
        dialog = str(extra.get("dialog") or "")
        shown = f" ({dialog})" if dialog else ""
        return (
            f"Power BI Desktop is showing a dialog{shown}. Finish or close it, then ask "
            "again. Nothing was changed."
        )
    if code == "edit_busy":
        return "Another change to this report is still being saved. Try again in a moment."
    if code == "edit_unsupported":
        found = [str(s) for s in extra.get("desktop_steps") or []] or list(steps)
        message = error.message.strip()
        message += "" if message.endswith((".", "!", "?")) else "."
        text = f"{message} Nothing was changed."
        return f"{text} To do it in Power BI Desktop:\n{_bullets(found[:4])}" if found else text
    if code == "desktop_unavailable":
        # each reason a sentence of its own ("... has not been checked with Studio yet.")
        given = [str(r).strip() for r in extra.get("reasons") or [] if str(r).strip()]
        given = given or [error.message.strip()]
        reasons = " ".join(r if r.endswith((".", "!", "?")) else f"{r}." for r in given)
        text = f"This needs Power BI Desktop, which Studio cannot use right now. {reasons} "
        text += "Nothing was changed."
        return f"{text} To do it yourself:\n{_bullets(list(steps)[:4])}" if steps else text
    if code == "edit_rejected" and _NO_CHANGE.search(error.message):
        # Not a failure and nothing to do in Desktop: the report is already the way asked.
        said = _STEP_PREFIX.sub("", error.message.strip()).rstrip(".")
        return f"{said}. The report was left as it is."
    if code == "edit_rejected":
        lowered = error.message.casefold()
        if action.verb == "undo" and "nothing to undo" in lowered:
            return "There is nothing to undo: no change has been made in this report yet."
        if action.verb == "restore" and "already the current version" in lowered:
            return "The report is already the file you imported, so there is nothing to restore."
        said = error.message.rstrip()
        said += "" if said.endswith((".", "!", "?")) else "."
        text = f"I could not make that change: {said} Nothing was changed."
        # a problem with the whole report (no editable definition), not one step's: the way
        # to do it in Desktop is the useful part
        return (
            f"{text} To do it in Power BI Desktop:\n{_bullets(list(steps)[:4])}" if steps else text
        )
    return error.message


def _receipt_lines(
    kind: str, summaries: Sequence[str], receipt: dict[str, Any], context: EditContext | None
) -> str:
    """The plain-English reply to a receipt: what was done, what to check, how to undo it."""
    if kind == "undo":
        first = _plain(summaries[0]) if summaries else "Went back one change."
    elif kind == "restore":
        first = _plain(summaries[0]) if summaries else "Restored an earlier version."
    elif len(summaries) > 1:
        first = "Done:\n" + _bullets(_capped(list(summaries), 8))
    else:
        first = f"Done. {summaries[0]}" if summaries else "Done."
    lines = [first]
    lines += [f"Note: {w}" for w in receipt.get("warnings") or []]
    where = {str(item.get("where")) for item in receipt.get("visible_in") or []}
    if "powerbi_only" in where:
        lines.append(
            "Power BI draws part of this change, but Studio's canvas does not yet: use "
            "Preview in Power BI to check it."
        )
    if "covered_by_background" in where:
        lines.append("Part of this change is behind the page's background image.")
    for item in receipt.get("protected") or []:
        found = context.visual(str(item.get("visual"))) if context is not None else None
        lines.append(
            f"Kept as it was: {item.get('what', 'formatting that follows the data')}"
            + (f" on {found.label}." if found is not None else ".")
        )
    if receipt.get("adopted"):
        lines.append(
            "First I saved the work you had done in Power BI Desktop as its own step in the "
            "history."
        )
    if receipt.get("outcome") == "partial":
        lines.append("The change is saved, but part of the follow-up did not finish (see above).")
    if receipt.get("replayed"):
        lines.append("(This request had already been applied.)")
    if kind == "edit":
        lines.append('Say "undo" to take it back.')
    return "\n".join(lines)


# --- operations ---------------------------------------------------------------------------------


@cache
def _operation_fields(name: str) -> frozenset[str]:
    for cls in report_edits.REPORT_OPERATIONS:
        if cls.model_fields["op"].default == name:
            return frozenset(cls.model_fields)
    return frozenset()


def _accepts(op: dict[str, Any], field_name: str) -> bool:
    return field_name in _operation_fields(str(op.get("op")))


def _with_flags(
    ops: Sequence[dict[str, Any]], flags: Sequence[tuple[str, int | None]]
) -> list[dict[str, Any]]:
    """Operations carrying the answers to the edit layer's confirmations (the kind names the
    flag, or ``_CONFIRM_FIELD`` does). A Yes belongs to the step that asked: the flag goes on
    that operation only, so it never replaces formatting the user was not asked about. When the
    edit layer did not say which step asked, every operation that takes the flag gets it."""
    made = [dict(op) for op in ops]
    for kind, step in flags:
        name = _CONFIRM_FIELD.get(kind, kind)
        only = (
            step - 1
            if isinstance(step, int) and 1 <= step <= len(ops) and _accepts(ops[step - 1], name)
            else None
        )
        for index, op in enumerate(ops):
            if _accepts(op, name) and (only is None or index == only):
                made[index][name] = True
    return made


_NO_CHANGE = re.compile(r"already looks like that", re.I)
_STEP_PREFIX = re.compile(r"^Step \d+:\s*")


def _without_no_change(action: Action, error: ImportedEditError) -> tuple[Action, str] | None:
    """The action without the one step the edit layer refused because the report already looks
    like that, and the line that says so; None when the refusal is anything else or is all
    there is to the action."""
    if action.verb != "apply" or error.code != "edit_rejected" or len(action.ops) < 2:
        return None
    step = error.extra.get("step")
    if not _NO_CHANGE.search(error.message) or not isinstance(step, int):
        return None
    if not 1 <= step <= len(action.ops):
        return None
    ops = tuple(op for index, op in enumerate(action.ops) if index != step - 1)
    flags = tuple(
        (kind, at - 1 if isinstance(at, int) and at > step else at)
        for kind, at in action.confirm_flags
        if at != step
    )
    said = _STEP_PREFIX.sub("", error.message.strip()).rstrip(".")
    # the summary described every step: the edit layer words the ones that remain
    trimmed = replace(action, ops=ops, confirm_flags=flags, summary="")
    return trimmed, f"Left one change out: {said}."


def _filled(
    ops: Sequence[dict[str, Any]], fill: dict[str, Any], step: object
) -> list[dict[str, Any]]:
    """The operations with the option's values written in: the step the edit layer named, or
    every operation that has all of those fields."""
    if not fill:
        return [dict(op) for op in ops]
    if isinstance(step, int) and 1 <= step <= len(ops):
        targets = {step - 1}
    else:
        targets = {i for i, op in enumerate(ops) if all(_accepts(op, key) for key in fill)} or {0}
    return [{**op, **fill} if i in targets else dict(op) for i, op in enumerate(ops)]


# --- the turn -----------------------------------------------------------------------------------


class _Turn:
    """One message (or one answer to a question) being carried out on its own thread."""

    def __init__(
        self,
        *,
        port: EditPort,
        env: ChatEditEnv,
        request: object,
        workspace_id: str,
        user_text: str,
        key: str,
        cancel: threading.Event,
        emit: Callable[[Event], None],
        context: EditContext,
        report: ImportedReport | None,
        snapshot: DatasetSnapshot | None,
        selection: str | None,
        page: str | None,
        said: str | None = None,
    ) -> None:
        self.port, self.env, self.request = port, env, request
        self.workspace_id, self.user_text, self.base_key = workspace_id, user_text, key
        self.said = said or user_text  # the words the clauses were read from
        self.cancel, self.emit = cancel, emit
        self.context, self.report, self.snapshot = context, report, snapshot
        self.selection, self.page = selection, page
        self.head: str | None = context.head_revision
        self.lines: list[str] = []
        self.saved: list[Event] = []  # the edit_* events to replay from the history
        self.extra_question: list[str] = []
        self.overview: list[dict[str, str]] = []
        self.halted = False  # a head-moving step asked or failed: the rest waits
        self.started = False  # a port call was made: the turn can no longer be abandoned
        self.aborted = False
        self.answered_what_if = False
        self.plan: PlanOutcome | None = None  # what the planner made, offered after the clauses
        self._calls = 0

    # -- plumbing --------------------------------------------------------------------------

    def _key(self) -> str:
        found = self.base_key if self._calls == 0 else f"{self.base_key}:{self._calls + 1}"
        self._calls += 1
        return found

    def _save(self, event: Event) -> None:
        self.saved.append(event)
        self.emit(event)

    def _progress(self, item: dict[str, Any]) -> None:
        self.emit(
            _event(
                "edit_progress",
                {"stage": str(item.get("stage", "applying")), "label": str(item.get("label", ""))},
            )
        )

    def _steps(self, text: str) -> list[str]:
        if self.report is None:
            return []
        try:
            return desktop_steps(text, self.report, self.snapshot)
        except Exception:  # noqa: BLE001 - steps are a courtesy; a refusal never depends on them
            return []

    # -- the port --------------------------------------------------------------------------

    def _call(self, action: Action, key: str) -> dict[str, Any]:
        head = self.head or str(self.port.head(self.workspace_id)["id"])
        if action.verb == "undo":
            return self.port.undo(
                self.workspace_id,
                expected_head=head,
                idempotency_key=key,
                progress=self._progress,
                desktop_work=action.desktop_work,
            )
        if action.verb == "restore":
            return self.port.restore(
                self.workspace_id,
                str(action.revision),
                expected_head=head,
                idempotency_key=key,
                confirmed_discard=action.confirmed_discard,
                progress=self._progress,
                desktop_work=action.desktop_work,
            )
        return self.port.apply(
            self.workspace_id,
            _with_flags(action.ops, action.confirm_flags),
            summary=action.summary[:300] or None,
            idempotency_key=key,
            expected_head=head,
            progress=self._progress,
            desktop_work=action.desktop_work,
            confirm_model=action.confirm_model,
            # A yes to tests the batch would break is batch-wide (``confirmed``); every other
            # confirmation is a flag on the operation that asked.
            confirm_replace=any(k != TESTS_CONFIRMATION for k, _ in action.confirm_flags),
            confirmed=tuple(k for k, _ in action.confirm_flags if k == TESTS_CONFIRMATION),
        )

    def execute(self, action: Action, clauses: Sequence[Clause] = ()) -> dict[str, Any] | None:
        """One port call. Returns its receipt, or None when it asked a question, failed, or was
        never started because the client had already gone."""
        if action.verb == "apply" and len(action.ops) > MAX_OPERATIONS:
            self.halted = True
            self.lines.append(
                f"That is {len(action.ops)} changes in one message, and one message can make "
                f"at most {MAX_OPERATIONS}. Nothing was changed: narrow it down (one page, or "
                "one kind of visual at a time) and ask again."
            )
            self._save(_event("edit_failed", {"message": self.lines[-1], "code": "too_many"}))
            return None
        if self.cancel.is_set() and not self.started:
            self.aborted = True  # the client left before anything was done: do nothing
            return None
        self.started = True
        left_out: list[str] = []
        while True:
            try:
                receipt = self._call(action, self._key())
            except ImportedEditError as error:
                # One step that changes nothing must not sink the others ("remove the legend
                # and hide the gridlines" when the gridlines are already hidden): it is left
                # out, said so, and the rest goes ahead.
                trimmed = _without_no_change(action, error)
                if trimmed is not None:
                    action = trimmed[0]
                    left_out.append(trimmed[1])
                    continue
                self.lines.extend(left_out)
                self._refused(error, action)
                return None
            except Exception:  # noqa: BLE001 - never a traceback in the chat
                LOGGER.exception("A chat edit failed unexpectedly")
                self.lines.extend(left_out)
                self._refused(ImportedEditError("edit_failed", GENERIC_FAILURE, 500), action)
                return None
            break
        self._applied(action, receipt, clauses)
        self.lines.extend(left_out)
        return receipt

    # -- the outcome -----------------------------------------------------------------------

    def _applied(self, action: Action, receipt: dict[str, Any], clauses: Sequence[Clause]) -> None:
        revision = receipt.get("revision") or {}
        head = receipt.get("head") or {}
        self.head = str(head.get("id") or revision.get("id") or self.head)
        kind = "edit" if action.verb == "apply" else action.verb
        summaries = [str(s) for s in receipt.get("summaries") or [] if s] or [
            c.summary for c in clauses if c.summary
        ]
        read = [c.summary for c in clauses if c.summary]
        if kind == "edit" and read and _leaves_out(action.ops, summaries):
            # the edit layer's line says less than was done ("Data labels on" for labels in
            # millions with 1 decimal): the words of what was read say all of it
            summaries = read
        if kind != "edit":
            summaries = [_plain(s) for s in summaries]
        if action.verb == "restore" and action.revision == ORIGINAL_ID:
            summaries = ["Restored the report to the file you imported."]
        self._save(
            _event(
                "edit_applied",
                {
                    "revision_id": revision.get("id"),
                    "parent_id": revision.get("parent"),
                    "head": head,
                    "kind": kind,
                    "ops": list(action.ops),
                    "summaries": summaries,
                    "warnings": list(receipt.get("warnings") or []),
                    "visible_in": list(receipt.get("visible_in") or []),
                    "protected": list(receipt.get("protected") or []),
                    "can_undo": True,
                    "outcome": receipt.get("outcome", "updated"),
                    "adopted": receipt.get("adopted"),
                    "desktop": receipt.get("desktop"),
                    "replayed": bool(receipt.get("replayed")),
                    "targets": [t.label for c in clauses for t in c.targets],
                    "clauses": self.overview,
                    "receipt": receipt,
                },
            )
        )
        self.lines.append(_receipt_lines(kind, summaries, receipt, self.context))
        if action.verb == "apply" and revision.get("id"):
            try:
                self.port.remember_last_edit(
                    self.workspace_id, str(revision["id"]), action.ops, " ".join(summaries)[:300]
                )
            except Exception:  # noqa: BLE001 - "undo the color change" then reads the history
                LOGGER.warning("Could not note the last edit", exc_info=True)
        if action.literal is not None:
            self._offer_stale_mentions(action.literal)

    def _refused(self, error: ImportedEditError, action: Action) -> None:
        """A port call that asked a question or failed: the matching event and a plain reply."""
        self.halted = True
        code = error.code
        if code == "needs_confirmation":
            self._ask_confirmation(error, action)
            return
        if code == "needs_choice" and self._ask_options(error, action):
            return
        if code == "desktop_unsaved":
            self._confirm(
                "desktop_unsaved", _UNSAVED_DETAIL, replace(action, desktop_work="save"), {}
            )
            return
        wants_steps = code in {"desktop_unavailable", "edit_unsupported"} or (
            code == "edit_rejected"
            and action.verb == "apply"
            and error.extra.get("step") is None
            and not _NO_CHANGE.search(error.message)
        )
        text = _problem_text(error, action, self._steps(action.text) if wants_steps else [])
        self.lines.append(text)
        self._save(_event("edit_failed", {"message": text, "code": code}))

    def _step_support(self, ops: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Per planned step: ``outside_supported`` (outside Microsoft's documented list of model
        changes for tools like Studio) and, when it is, ``verified_on`` (the installed Desktop
        version that passed Studio's model probe, else None: the edit will be refused). The
        service decides it as the editor's gate does; a port without that answer (a test fake)
        gets the operation's own tag and no version."""
        ask = getattr(self.port, "model_step_support", None)
        if callable(ask):
            try:
                found = ask(self.workspace_id, ops)
                if isinstance(found, list) and len(found) == len(ops):
                    return [dict(item) for item in found]
            except Exception:  # noqa: BLE001 - a plan is still shown; Apply is gated anyway
                LOGGER.warning("Could not say which plan steps Microsoft supports", exc_info=True)
        tags = report_edit_model.MICROSOFT_SUPPORT
        return [
            {"outside_supported": True, "verified_on": None}
            if tags.get(str(op.get("op"))) == "outside"
            else {"outside_supported": False}
            for op in ops
        ]

    def _ask_confirmation(self, error: ImportedEditError, action: Action) -> None:
        kind = str(error.extra.get("kind") or "replace_conditional")
        detail = str(error.extra.get("detail") or error.message)
        if kind == "model_edit":
            if _GENERIC_MODEL_LINE in detail and action.summary:
                # the edit layer words only measure changes ("Change the model." for the rest):
                # the step's own line says what will change ("Hide DimStore[StoreKey] ...")
                detail = detail.replace(_GENERIC_MODEL_LINE, action.summary.strip()).strip()
            self._confirm(
                "model_edit",
                f"{detail} {model_edit_note(action.ops)}",
                replace(action, confirm_model=True),
                {},
            )
        elif kind == "restore_discard":
            rows = [r for r in error.extra.get("would_discard") or [] if isinstance(r, dict)]
            listed = _capped([_history_line(r) for r in rows], HISTORY_LINES)
            many = "changes" if len(rows) != 1 else "change"
            found = "the file you imported" if action.revision == ORIGINAL_ID else "that version"
            self._confirm(
                "restore",
                f"Going back to {found} leaves out {len(rows)} later {many}:\n{_bullets(listed)}",
                replace(action, confirmed_discard=True),
                {"would_discard": rows},
            )
        else:
            step = error.extra.get("step")
            asked = (kind, step if isinstance(step, int) else None)
            # Tests the batch would break are listed with the question, so the card can show them.
            tests = error.extra.get("tests")
            self._confirm(
                kind,
                detail,
                replace(action, confirm_flags=(*action.confirm_flags, asked)),
                {"tests": tests} if tests is not None else {},
            )

    def _confirm(self, kind: str, detail: str, action: Action, extra: dict[str, Any]) -> None:
        pending = Pending(
            self.workspace_id, "confirm", self.head, self.selection, self.page, action=action
        )
        data: dict[str, Any] = {
            "kind": kind,
            "ops": list(action.ops),
            "detail": detail,
            "pending_id": self.env.pending.add(pending),
            **extra,
        }
        if action.what_if:
            data["what_if"] = dict(action.what_if)
        self._save(_event("edit_confirm", data))
        self.lines.append(f"{detail}\n\nNothing has changed yet. Confirm to go ahead.")

    def _ask_options(self, error: ImportedEditError, action: Action) -> bool:
        rows = [r for r in error.extra.get("choices") or [] if isinstance(r, dict)]
        options = tuple(
            Option(
                str(row.get("label", "")),
                replace(
                    action,
                    ops=tuple(_filled(action.ops, dict(row["fill"]), error.extra.get("step"))),
                ),
            )
            for row in rows
            if isinstance(row.get("fill"), dict) and row.get("label")
        )
        if not options:
            return False
        self._ask_with(str(error.extra.get("question") or error.message), options)
        return True

    def _ask_with(self, question: str, options: tuple[Option, ...]) -> None:
        pending = Pending(
            self.workspace_id, "options", self.head, self.selection, self.page, options=options
        )
        self._save(
            _event(
                "edit_choices",
                {
                    "question": question,
                    "choices": [{"index": i, "label": o.label} for i, o in enumerate(options)],
                    "pending_id": self.env.pending.add(pending),
                },
            )
        )
        self.lines.append(question)

    # -- stale numbers after a measure edit -----------------------------------------------

    def _offer_stale_mentions(self, literal: tuple[float, float]) -> None:
        """Captions and measure names that still say the old number (P32). Renaming measures is
        a Desktop cycle, so it is offered as a choice that leads to its own confirmation. A
        caption is a title, which Studio does not change: it is named, with where to change it."""
        try:
            fresh = self.port.edit_context(self.workspace_id, None, None)
        except Exception:  # noqa: BLE001 - the offer is a courtesy
            return
        old, new = literal
        found = stale_literal_mentions(old, fresh, new)
        captions = [m for m in found if m.kind == "caption"][:8]
        measures = [m for m in found if m.kind == "measure" and m.ops][:8]
        saying = short_number(new)
        if captions:
            shown_captions = _capped([f"'{m.text}'" for m in captions], 5)
            self.lines.append(
                f"These captions still say {short_number(old)}, while the report now uses "
                f"{saying}: {', '.join(shown_captions)}. Studio doesn't change titles; change "
                "them in Power BI Desktop's Format pane."
            )
        if not measures:
            return
        many = "s" if len(measures) != 1 else ""
        options = (
            Option(
                f"Rename {len(measures)} measure{many} (needs Power BI, about 1 minute)",
                Action(
                    ops=tuple(op for m in measures for op in m.ops),
                    summary=f"Renamed {len(measures)} measure{many} to say {saying}.",
                    text="rename the measures",
                ),
            ),
            Option("Leave them as they are", None),
        )
        shown = _capped([f"measure '{m.text}' -> '{m.suggestion}'" for m in measures], 5)
        self._ask_with(
            f"Some measure names still say {short_number(old)}, while the report now uses "
            f"{saying}. Rename them too?\n{_bullets(shown)}",
            options,
        )

    # -- clauses ---------------------------------------------------------------------------

    def process(self, clauses: list[Clause]) -> None:
        self.overview = [
            {"text": c.text, "status": _STATUS_WORD.get(c.kind, "done"), "summary": c.summary}
            for c in clauses
            if c.kind is not ParseKind.NOT_AN_EDIT
        ]
        applies = [c for c in clauses if c.kind is ParseKind.APPLY]
        models = [c for c in clauses if c.kind is ParseKind.MODEL_EDIT]
        places = [i for i, c in enumerate(clauses) if c.kind is ParseKind.APPLY]
        # The edits are read again after an undo only when they sit side by side in the message
        # (one span of words to read); scattered ones keep the operations they were read with.
        together = bool(places) and all(
            clauses[i].kind is ParseKind.APPLY for i in range(places[0], places[-1] + 1)
        )
        batched = moved = False
        for clause in clauses:
            kind = clause.kind
            if kind is ParseKind.APPLY:
                if not batched:
                    batched = True
                    self._batch(applies, moved and together)
            elif kind in {ParseKind.UNDO, ParseKind.REDO, ParseKind.RESTORE}:
                moved = self._history_step(clause) or moved
            elif kind is ParseKind.WHAT_CHANGED:
                self._what_changed()
            elif kind is ParseKind.UNSUPPORTED:
                self._unsupported(clause)
            elif kind is ParseKind.CLARIFY:
                self._ask(clause)
        self._model_edits(models)

    def _batch(self, applies: list[Clause], moved: bool) -> None:
        if self.halted:
            self.lines.append(_SKIPPED)
            return
        if moved:
            # An undo or restore moved the head: the edits after it were read against the
            # report as it was, so they are read again against the report as it is.
            self.context = self.port.edit_context(self.workspace_id, self.selection, self.page)
            self.head = self.context.head_revision
            again = parse_edit_message(self._words_of(applies), self.context)
            actionable = [c for c in again.clauses if c.kind is not ParseKind.NOT_AN_EDIT]
            if not actionable:
                self.lines.append(
                    "After that step I could not read the rest of your message again. Ask for "
                    "it once more."
                )
                return
            self.process([c for c in again.clauses if c.kind not in _HISTORY_KINDS])
            return
        ops = tuple(op for c in applies for op in c.ops)
        if not ops:
            self.lines.extend(c.summary for c in applies if c.summary)
            return
        summary = " ".join(c.summary for c in applies if c.summary)
        text = " ".join(c.text for c in applies)
        self.execute(Action(ops=ops, summary=summary, text=text), applies)

    def _words_of(self, applies: Sequence[Clause]) -> str:
        """The user's own words for these edits, separators and all. The clause texts must not
        be joined with a space: "make it green, make the title bigger" then reads as ONE edit
        and the parser applies the wrong operations (the color goes to the title)."""
        said = self.said
        first = _find_words(said, applies[0].text)
        last = _find_words(said, applies[-1].text, first.start() if first else 0)
        if first is not None and last is not None and last.end() >= first.end():
            return said[first.start() : last.end()]
        return ". ".join(c.text.strip().rstrip(".,;") for c in applies)

    def _history_step(self, clause: Clause) -> bool:
        """Undo, redo or restore. True when the head moved."""
        if self.halted:
            self.lines.append(_SKIPPED)
            return False
        if clause.kind is ParseKind.UNDO and _UNDO_OF_UNDO.search(clause.text):
            # "undo the undo" takes the undo back: a redo. Running another undo instead would
            # silently drop the user's earlier work.
            if len(_UNDO_WORD.findall(clause.text)) > 2:
                self.lines.append(
                    'I am not sure how far you want to go back and forth. Say "undo" or "redo" '
                    "once."
                )
                return False
            clause = replace(clause, kind=ParseKind.REDO)
        if clause.kind is ParseKind.UNDO:
            action = Action("undo", summary=clause.summary, text=clause.text)
        elif clause.kind is ParseKind.RESTORE:
            action = Action(
                "restore", revision=ORIGINAL_ID, summary=clause.summary, text=clause.text
            )
        else:
            target = self._redo_target()
            if target is None:
                self.lines.append(
                    "There is nothing to redo: the latest change in this report has not been "
                    "undone."
                )
                return False
            action = Action("restore", revision=target, summary=clause.summary, text=clause.text)
        return self.execute(action) is not None

    def _redo_target(self) -> str | None:
        """The change an undo took back, while that undo is still the head (the rule of the
        History pane's redo)."""
        rows = self.port.history(self.workspace_id)
        head = next((r for r in rows if r.get("is_head")), rows[0] if rows else None)
        if head is None or head.get("author") != "undo" or not head.get("parent"):
            return None
        target = next((r for r in rows if r.get("id") == head["parent"]), None)
        if target is None or target.get("available") is False:
            return None
        return str(target["id"])

    def _what_changed(self) -> None:
        rows = self.port.what_changed(self.workspace_id, HISTORY_LINES)
        if not rows:
            self.lines.append(
                "No changes have been made to this report yet: it is still the file you imported."
            )
            return
        lines = [_history_line(row, dated=True) for row in rows]
        self.lines.append("Here is what changed, newest first:\n" + _bullets(lines))

    def _unsupported(self, clause: Clause) -> None:
        steps = list(clause.desktop_steps)
        if not steps and clause.capability != NOT_AVAILABLE:
            steps = self._steps(clause.text)
        self.lines.append(_unsupported_text(clause, steps))

    def _ask(self, clause: Clause) -> None:
        if not clause.choices:
            self.lines.append(clause.question)
            return
        pending = Pending(
            self.workspace_id,
            "clarify",
            self.head,
            self.selection,
            self.page,
            clause=clause,
        )
        self._save(
            _event(
                "edit_choices",
                {
                    "question": clause.question,
                    "choices": [
                        {"index": i, "label": c.label} for i, c in enumerate(clause.choices)
                    ],
                    "pending_id": self.env.pending.add(pending),
                },
            )
        )
        self.lines.append(clause.question)

    def _model_edits(self, clauses: list[Clause]) -> None:
        """Measure changes: the what-if first, then a confirmation; never applied here."""
        if not clauses:
            return
        ops: list[dict[str, Any]] = []
        summaries: list[str] = []
        texts: list[str] = []
        what_if: dict[str, str] | None = None
        literal: tuple[float, float] | None = None
        hint = False
        for clause in clauses:
            answer = None
            if clause.what_if is not None and not self.halted:
                answer = self.env.what_if(clause.text) or self.env.what_if(clause.what_if.question)
                answer = _WHAT_IF_FOOTER.sub("", answer).rstrip() if answer else None
            if answer:
                self.answered_what_if = True
                self.lines.append(answer)
                assert clause.what_if is not None
                what_if = what_if or {"question": clause.what_if.question, "answer": answer}
            if not clause.ops:
                if answer:
                    hint = True
                else:
                    self.extra_question.append(clause.text)  # the question paths may still answer
                continue
            ops += clause.ops
            summaries.append(clause.summary)
            texts.append(clause.text)
            literal = literal or self._changed_literal(clause)
        if hint and not ops:
            self.lines.append(
                'Nothing has changed. To apply it, say for example "change the target to 120K".'
            )
        if not ops:
            return
        if self.halted:
            self.lines.append(_SKIPPED)
            return
        self.execute(
            Action(
                ops=tuple(ops),
                summary=" ".join(summaries),
                text=" ".join(texts),
                what_if=what_if,
                literal=literal,
            )
        )

    def _changed_literal(self, clause: Clause) -> tuple[float, float] | None:
        """The (old, new) number a measure edit changes, to find captions that still say it."""
        what = clause.what_if
        measure = self.context.measure(what.measure) if what is not None else None
        if what is None or measure is None:
            return None
        before = numeric_literals(measure.expression)
        after = numeric_literals(what.new_expression)
        if len(before) != len(after):
            return None
        for (old, _, _), (new, _, _) in zip(before, after, strict=True):
            if old != new:
                return old, new
        return None

    # -- the 4B tier -----------------------------------------------------------------------

    def _planning(self, label: str) -> None:
        self.emit(_event("edit_progress", {"stage": "planning", "label": label}))

    def model_tier(self, unparsed: Sequence[str], name_the_piece: bool = False) -> Clause | None:
        """The planner for the pieces the deterministic tier could not read (P30): the local
        model chooses tool cards and fills their slots in one call. A plan is kept in
        ``self.plan`` and offered after the other clauses (``edit_plan``, applied only on
        Apply); a question or a refusal comes back as a clause; anything else is chips. With
        ``name_the_piece`` (the message had other parts), the chips say which part they are
        for, so a reply never shows them after a done part without saying what failed."""
        text = unparsed[0]
        failed = (
            f"I could not read this part of your message: “{text}”."
            if name_the_piece or len(unparsed) > 1
            else ""
        )
        if self.cancel.is_set() or self.env.model is None:
            for later in unparsed[1:]:
                self.lines.append(f"I could not read this part of your message: “{later}”.")
            return fallback_clause(text, self.context, failed)
        started = time.monotonic()
        try:
            # generate() releases the guarded runtime itself when it times out or fails, so a
            # slow or cancelled attempt never leaves the model locked.
            outcome = EditPlanner(
                self.context,
                self.env.model,
                progress=self._planning,
                cancelled=self.cancel.is_set,
                pattern_catalog=self.env.pattern_catalog,
                pattern_preview=self.env.pattern_preview,
                formula_request=self.user_text,
            ).plan(unparsed)
        except Exception as error:  # noqa: BLE001 - any failure means suggestions, never an edit
            LOGGER.exception("Planning a chat edit failed")
            outcome = PlanOutcome("failed", reason=f"planner: {str(error)[:160]}")
        seconds = round(time.monotonic() - started, 1)
        for later in outcome.left_out:
            self.lines.append(f"I could not read this part of your message: “{later}”.")
        if outcome.kind == "failed":
            self.env.log(
                "chat.edit_model_failed",
                {"reason": outcome.reason[:200], "seconds": seconds, "calls": outcome.calls},
            )
            for later in unparsed[1:]:
                if later not in outcome.left_out:
                    self.lines.append(f"I could not read this part of your message: “{later}”.")
            retry = _model_retry_message(outcome.reason)
            if retry:
                return Clause(
                    text,
                    ParseKind.CLARIFY,
                    question=" ".join(part for part in (failed, retry) if part),
                )
            return fallback_clause(text, self.context, failed)
        self.env.log(
            "chat.edit_model_read",
            {
                "kind": outcome.kind,
                "steps": [step.op for step in outcome.steps],
                "seconds": seconds,
                "calls": outcome.calls,
                "prompt_bytes": outcome.prompt_bytes,
            },
        )
        if outcome.kind == "plan":
            self.plan = outcome
            return None
        return outcome.clause

    def _offer_plan(self, outcome: PlanOutcome) -> None:
        """The plan as a preview (``edit_plan``) with an Apply that is the open question's yes:
        ``POST .../chat/edit-confirm`` applies every step as one revision. The Apply is also the
        confirmation of a model change the plan lists (it says so, and Desktop is needed)."""
        steps = list(outcome.steps)
        ops = tuple(step.operation for step in steps)
        many = len(steps) != 1
        lines = [f"Here is what I would do ({'one step' if not many else f'{len(steps)} steps'}):"]
        lines.append(_bullets([step.summary for step in steps]))
        notes = [note for step in steps for note in step.notes] + list(outcome.notes)
        lines += list(dict.fromkeys(notes))
        what_if, literal = self._plan_what_if(steps)
        if what_if is not None:
            lines.append(what_if["answer"])
        model_ops = [step.operation for step in steps if step.needs_desktop]
        if model_ops:
            lines.append(model_edit_note(model_ops))
            if len(model_ops) < len(steps):
                # one revision: a Desktop step that cannot run stops the report steps too
                lines.append(
                    "The steps are applied together: if Power BI Desktop cannot make the model "
                    "change, none of them is applied."
                )
        confirming = [step.summary for step in steps if step.confirm and not step.needs_desktop]
        if confirming:
            lines.append("Apply is your yes to: " + "; ".join(confirming))
        detail = "\n".join(lines)
        action = Action(
            ops=ops,
            summary=" ".join(step.summary for step in steps),
            text=self.said,
            confirm_model=outcome.needs_desktop,
            what_if=what_if,
            literal=literal,
        )
        pending_id = self.env.pending.add(
            Pending(
                self.workspace_id, "confirm", self.head, self.selection, self.page, action=action
            )
        )
        support = self._step_support([step.operation for step in steps])
        plan: dict[str, Any] = {
            "steps": [
                {
                    "summary": step.summary,
                    "op": step.op,
                    "needs_desktop": step.needs_desktop,
                    "confirm": step.confirm,
                    "source": step.source,
                    **known,
                }
                for step, known in zip(steps, support, strict=True)
            ],
            "ops": list(ops),
            "detail": detail,
            "needs_desktop": outcome.needs_desktop,
            "left_out": list(outcome.left_out),
            "pending_id": pending_id,
        }
        if what_if is not None:
            plan["what_if"] = dict(what_if)
        self._save(_event("edit_plan", plan))
        # Until the desktop app draws ``edit_plan``, the same question also comes as the card it
        # does draw: an ``edit_confirm`` of kind "plan" with the same pending id (its Apply and
        # Cancel answer it the same way). A client that draws edit_plan skips this one.
        confirm: dict[str, Any] = {
            "kind": "plan",
            "ops": list(ops),
            "detail": detail,
            "pending_id": pending_id,
            "steps": plan["steps"],
        }
        if what_if is not None:
            confirm["what_if"] = dict(what_if)
        self._save(_event("edit_confirm", confirm))
        them = "these changes" if many else "this change"
        self.lines.append(
            f"{detail}\n\nNothing has changed yet. Choose Apply to report to make {them} as one "
            "step you can undo, or Cancel."
        )

    def _plan_what_if(
        self, steps: Sequence[Any]
    ) -> tuple[dict[str, str] | None, tuple[float, float] | None]:
        """The local what-if for a plan that changes a measure's formula (P32, as for a measure
        change the parser read): Studio's own numbers before the confirmation; and the (old,
        new) number it changes, to offer updating captions that still say the old one."""
        for step in steps:
            operation = step.operation
            if operation.get("op") != "update_measure" or not operation.get("expression"):
                continue
            literal: tuple[float, float] | None = None
            measure = self.context.measure(str(operation.get("name", "")))
            if measure is not None:
                before = numeric_literals(measure.expression)
                after = numeric_literals(str(operation["expression"]))
                if len(before) == len(after):
                    changed = [(o, n) for (o, _, _), (n, _, _) in zip(before, after, strict=True)]
                    literal = next(((o, n) for o, n in changed if o != n), None)
            if self.halted:
                return None, literal
            answer = self.env.what_if(self.said)
            answer = _WHAT_IF_FOOTER.sub("", answer).rstrip() if answer else None
            if answer:
                self.answered_what_if = True
                return {"question": self.said, "answer": answer}, literal
            return None, literal
        return None, None

    # -- the end ---------------------------------------------------------------------------

    def _record(self, text: str) -> None:
        try:
            self.env.record(
                self.request,
                self.workspace_id,
                self.user_text,
                {"text": text, "draft_id": None, "edit_events": self.saved},
            )
        except Exception:  # noqa: BLE001 - the reply is still shown; the log has the reason
            LOGGER.warning("Could not record a chat edit in the history", exc_info=True)

    def finish(self, remainder: str = "", presentation: bool = False) -> None:
        """Record the turn, say it, then hand what is left to the question paths."""
        if self.aborted:
            return
        text = "\n\n".join(part for part in self.lines if part)
        recorded = False
        if text:
            self._record(text)
            self.emit(_token(text))
            recorded = True
        question = " ".join(p for p in (remainder, *self.extra_question) if p).strip()
        # "Thanks!" after an edit is no question: it gets no model call
        if (
            question
            and (not recorded or presentation or _ASKS.search(question))
            and not self.cancel.is_set()
        ):
            if recorded:
                self.emit(_token("\n\n"))
            asked = Question(
                question,
                edited=recorded,
                presentation=presentation,
                turn=not recorded,
                allow_model=not self.answered_what_if,
            )
            for item in self.env.answer(asked, self.cancel):
                self.emit(item)

    def guarded(self, work: Callable[[], None]) -> None:
        """``work``, with anything it raises turned into the turn's own failure reply."""
        try:
            work()
        except ImportedEditError as error:
            self._refused(error, Action())
        except Exception:  # noqa: BLE001 - never a traceback in the chat
            LOGGER.exception("Reading a chat edit failed")
            self._refused(ImportedEditError("edit_failed", GENERIC_FAILURE, 500), Action())

    def run(self, result: ParseResult, presentation: bool, note: str = "") -> None:
        def work() -> None:
            clauses = list(result.clauses)
            if result.unparsed:
                others = len(result.unparsed) > 1 or any(
                    c.kind is not ParseKind.NOT_AN_EDIT for c in clauses
                )
                made = self.model_tier(result.unparsed, name_the_piece=others)
                if made is not None:
                    clauses.append(made)
                held = [c for c in clauses if c.kind is ParseKind.APPLY and c.ops]
                if (
                    self.plan is not None
                    and held
                    and not any(c.kind in _HISTORY_KINDS for c in clauses)
                ):
                    # One message, one preview: the clear edits wait with the planned ones, so
                    # nothing applies before the person has seen every step (one Apply, one
                    # revision, one Undo).
                    self.plan = _with_clear_steps(self.plan, held)
                    clauses = [c for c in clauses if c not in held]
            self.process(clauses)
            if self.plan is not None:
                self._offer_plan(self.plan)

        self.guarded(work)
        if note and self.saved:
            self.lines.append(note)
        self.finish(result.remainder, presentation)

    def plain(self, text: str, *, code: str | None = None) -> None:
        """A one-line outcome of an answer (expired, declined, not available)."""
        self.lines.append(text)
        if code is not None:
            self._save(_event("edit_failed", {"message": text, "code": code}))


# Operation fields a receipt needs not name: the target, and on/off that the line always says.
_UNSAID_FIELDS = frozenset(
    {"op", "visual", "visuals", "page", "show", "hidden", "target", "axis", "scope", "mode"}
)
_FIELD_WORDS = {
    "display_units": ("thousand", "million", "billion", "unit"),
    "decimals": ("decimal",),
}


def _leaves_out(ops: Sequence[Mapping[str, Any]], summaries: Sequence[str]) -> bool:
    """Whether the receipt's lines leave out a setting the operations changed (display units,
    decimals...): each such field must be named by a word of it or of its value."""
    said = " ".join(summaries).casefold()
    for op in ops:
        for key, value in op.items():
            if key in _UNSAID_FIELDS or key.endswith("_flags") or value is None:
                continue
            words = _FIELD_WORDS.get(key) or tuple(key.split("_"))
            shown = {str(value).casefold()}
            if isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                shown.add((hue_family(value) or value).casefold())  # "#107C10" is said "green"
            if not any(w in said for w in words) and not any(s in said for s in shown):
                return True
    return False


def _with_clear_steps(plan: PlanOutcome, clauses: Sequence[Clause]) -> PlanOutcome:
    """The plan with the edits the parser read in the same message in front of it (source
    "parser"): they wait for the same Apply."""
    steps: list[PlanStep] = []
    for clause in clauses:
        for n, operation in enumerate(clause.ops, 1):
            summary = clause.summary if len(clause.ops) == 1 else f"{clause.summary} ({n})"
            steps.append(
                PlanStep(
                    op=str(operation.get("op")),
                    operation=dict(operation),
                    summary=summary or clause.text,
                    source="parser",
                )
            )
    return replace(plan, steps=(*steps, *plan.steps))


_HISTORY_KINDS = frozenset(
    {ParseKind.UNDO, ParseKind.REDO, ParseKind.RESTORE, ParseKind.WHAT_CHANGED}
)
_UNDO_WORD = re.compile(r"\bundo\b", re.I)
_UNDO_OF_UNDO = re.compile(
    r"\bundo\s+(?:(?:the|that|this|my|your|last|latest|previous)\s+)+(?:\w+\s+)?undo\b", re.I
)


def _find_words(text: str, words: str, start: int = 0) -> re.Match[str] | None:
    """Where ``words`` (a clause's text) sit in the user's message, ignoring case and spacing."""
    parts = words.split()
    if not parts:
        return None
    return re.compile(r"\s+".join(re.escape(p) for p in parts), re.I).search(text, start)


_STATUS_WORD = {
    ParseKind.CLARIFY: "asked",
    ParseKind.UNSUPPORTED: "unsupported",
    ParseKind.MODEL_EDIT: "needs_confirmation",
}


# --- suggestion chips ---------------------------------------------------------------------------


def _model_retry_message(reason: str) -> str:
    """Known runtime failures need recovery advice, not unrelated visual edits.

    Match only the planner's exception prefix and known runtime messages. An invalid
    model plan still follows the clarification path, and internal paths or arbitrary
    exception text stay in the audit log. A stopped session does not establish why it
    was stopped; in particular, it must never be described as a RAM failure.
    """
    prefix, separator, detail = reason.partition(":")
    if not separator or prefix not in {"model", "planner"}:
        return ""
    detail = detail.strip().casefold()
    validation = "local model failed strict schema/artifact validation: "
    if detail.startswith(validation):
        detail = detail.removeprefix(validation)
    if detail.startswith("local model is busy"):
        return "Local AI is busy with another request. Wait for it to finish, then try again."
    if detail.startswith("local model is starting"):
        return "Local AI is starting. Wait until it is ready, then try again."
    if detail.startswith(
        (
            "model session unloaded",
            "model session closed",
            "model session is closed",
            "guarded model session stopped",
            "model request has ended",
            "stop requested",
        )
    ):
        return "Local AI stopped before the plan was ready. Send the request again when ready."
    if detail.startswith(("timed out", "120-second generation/load cap reached")) or detail in {
        "generation ended timeout",
        "local model response did not finish: timeout",
    }:
        return "Local AI did not finish in time. Try again, or ask for one change at a time."
    if detail in {"generation ended length", "local model response did not finish: length"}:
        return "The plan exceeded Local AI's response limit. Ask for one change at a time."
    if detail.startswith(
        ("qwen 4b is not installed", "no model is loaded", "warm model profile is unavailable")
    ):
        return "Local AI is unavailable. Check the local model in Settings, then try again."
    if detail.startswith(
        (
            "context budget exceeded",
            "context budget cannot fit a repair",
            "context input exceeds the bounded tokenizer request",
        )
    ):
        return "This request is too large for Local AI. Ask for one change at a time."
    if detail.startswith(
        (
            "owned model process tree exceeded its profile memory cap",
            "llama-server exceeded 6 gib model memory cap",
        )
    ):
        return (
            "Local AI exceeded its memory limit. Try a smaller request or enter the change "
            "explicitly."
        )
    if detail.startswith(
        (
            "runtime/model hash mismatch",
            "runtime hash mismatch",
            "adaptive profile configuration hash mismatch",
            "adaptive profile identity mismatch",
            "missing runtime file",
            "text-only runtime file is not pinned",
        )
    ):
        return (
            "Local AI's runtime files could not be verified. Check the local model in Settings "
            "before retrying."
        )
    return ""


def _similar(context: EditContext, message: str) -> list[VisualEntry]:
    """The visuals a message most resembles, on the page it is about: the selected one first."""
    words = set(tokens(message))
    chosen = context.visual(context.selection) if context.selection else None
    page = context.page(chosen.page_id) if chosen else None
    page = page or (context.page(context.active_page) if context.active_page else None)
    page = page or (context.pages[0] if context.pages else None)
    if page is None:
        return [chosen] if chosen else []
    scored: list[tuple[int, int, VisualEntry]] = []
    for position, entry in enumerate(context.visuals_on(page.id)):
        if entry.hidden or entry.is_group or (chosen is not None and entry.id == chosen.id):
            continue
        texts = [entry.caption, *entry.fields, type_name(entry.visual_type)]
        score = sum(len(words & set(tokens(text))) for text in texts if text)
        if score:
            scored.append((-score, position, entry))
    found = [entry for _, _, entry in sorted(scored, key=lambda item: item[:2])[:2]]
    return ([chosen] if chosen is not None else []) + found


def fallback_clause(message: str, context: EditContext, reason: str = "") -> Clause:
    """A question with suggestion chips for a message nobody could read: the visuals it most
    resembles and a few common edits, each ready to apply. Nothing is applied without a click."""
    parser = Parser(context)
    choices: list[Choice] = []
    for visual in _similar(context, message):
        for label, sentence in _CHIP_EDITS:
            if len(choices) >= MAX_CHIPS:
                break
            try:
                made = card_clause(sentence, replace(context, selection=visual.id))
                if made is None:
                    made = parser.clause(sentence, forced=Forced(visuals=[visual.id]))
            except Exception:  # noqa: BLE001, S112 - a chip that cannot be built is left out
                continue
            if made is not None and made.kind is ParseKind.APPLY and made.ops:
                shown = label.format(t=visual.label)
                choices.append(Choice(shown, ops=made.ops, summary=made.summary))
    lead = f"{reason.rstrip()} " if reason else ""
    if choices:
        question = (
            f"{lead}I could not tell exactly what to change. Pick one of these, or say it as "
            'one short sentence, for example "move the chart left".'
        )
    else:
        question = (
            f"{lead}I could not tell what to change. Name the visual by its title or select it "
            'on the canvas, then say what to change, for example "move the chart left".'
        )
    return Clause(message, ParseKind.CLARIFY, question=question, choices=tuple(choices))


# --- entry points -------------------------------------------------------------------------------


class _Failure:
    def __init__(self, error: BaseException) -> None:
        self.error = error


_END = object()


def _spawn(
    produce: Callable[[Callable[[Event], None]], None], cancel: threading.Event
) -> Iterator[Event]:
    """Run ``produce`` on its own thread and relay what it emits. The thread is not stopped when
    the client goes away: only the relay ends, so a running batch always finishes and records
    its outcome (P8)."""
    channel: queue.Queue[Any] = queue.Queue()

    def work() -> None:
        try:
            produce(channel.put)
        except BaseException as error:  # noqa: BLE001 - re-raised on the consumer's thread
            channel.put(_Failure(error))
        finally:
            channel.put(_END)

    # The reserved chat owner is scoped to each generator advance. Preserve it
    # for this worker so an edit's follow-up answer belongs to the same turn;
    # an unrelated request still cannot acquire the active workspace lease.
    context = copy_context()
    threading.Thread(target=context.run, args=(work,), name="chat-edit", daemon=True).start()

    def relay() -> Iterator[Event]:
        while True:
            try:
                item = channel.get(timeout=0.2)
            except queue.Empty:
                if cancel.is_set():
                    return
                continue
            if item is _END:
                return
            if isinstance(item, _Failure):
                raise item.error
            yield item

    return relay()


_TRAILING_QUESTION = re.compile(
    r"^(?P<edit>[^\n]*?\S)\s*(?:,\s*|\s)(?:and|then|also)\s+"
    r"(?P<question>(?!what\s+if\b)(?:what|which|who|whom|whose|how|why|when|where|is|are|do|does)"
    r"\b[^\n]*\?)\s*$",
    re.I,
)


def _rewritten(result: ParseResult) -> bool:
    """Whether the parser turned a sentence into a question ("make a table of the top 5 agents"
    asks for figures, not a new visual)."""

    def flat(text: str) -> str:
        return " ".join(text.split()).casefold()

    said = [c.text for c in result.clauses if c.kind is ParseKind.NOT_AN_EDIT]
    return bool(said) and bool(result.remainder) and flat(result.remainder) != flat(" ".join(said))


# Words that ask for a change of the report. The 4B tier is only for text with one of these (or
# a complaint about how something looks): "I need this for the board meeting" is a statement,
# and reading it as an edit request is how a hallucinated intent would be offered as a change.
_EDIT_VERBS = (
    r"make|change|set|turn|use|add|remove|delete|hide|show|move|resize|rename|retitle|fix|update"
    r"|put|give|apply|swap|replace|align|sort|filter|group|colou?r|recolou?r|format|enlarge|shrink"
    r"|increase|decrease|duplicate|copy|drop|center|centre|stretch|widen|narrow|shift|clean|tidy"
    r"|improve|style|switch|convert|reorder|arrange|rotate|fill|paint|dim|highlight|shorten"
    r"|lengthen|reduce|enable|disable|toggle|expand|collapse|lower|raise|unhide|bold|italicise"
    r"|italicize|underline|resort|redo|restyle|reformat|tweak|adjust|edit|insert|create|build"
    r"|nudge|push|pull|drag|reposition|space|round|wrap|truncate|abbreviate|stack|unstack"
)
_EDIT_LIKE = re.compile(
    rf"\b(?:{_EDIT_VERBS})\b"
    r"|\b(?:bring|send)\b[^\n]*\b(?:front|back|forward|backward)\b"
    r"|\btoo\s+(?:small|big|large|dark|light|bright|busy|cluttered|wide|narrow|tall|short|long)\b"
    r"|\b(?:should|needs?\s+to|has\s+to|must)\s+be\b|\binstead\b"
    r"|\b(?:hard|difficult)\s+to\s+(?:read|see)\b|\bcan(?:not|'t)\s+(?:read|see)\b",
    re.I,
)
_LEADING_QUESTION = re.compile(
    r"^(?P<question>(?:first\s+|also\s+)?(?:(?:tell|show|give)\s+me\b|(?:what|which|who|whom|whose"
    r"|how|why|when|where|is|are|do|does)\b)[^\n]*?)"
    r"(?:\s*,\s*(?:and\s+)?(?:then\s+)?|\s+and\s+(?:then\s+)?|\s+then\s+|\s+also\s+)"
    rf"(?P<edit>(?:please\s+)?(?:{_EDIT_VERBS})\b[^\n]*)$",
    re.I,
)
_ASKING = re.compile(r"^(?:first\s+|also\s+)?(?:tell|show|give)\s+me\b", re.I)
# An edit that takes free text (a title, a caption, a name): the words after "to" are the text,
# question mark and all, so a question tacked on with "and" cannot be told apart from the text.
_TEXT_EDIT = re.compile(
    r"\b(?:set|change|rename|retitle|update|edit|call)\b[^\n]*?"
    r"\b(?:title|caption|subtitle|heading|label|name|text|header)\b[^\n]*?"
    r"\b(?:to|as)\s+(?!(?:the|a|an|this|that|it|my|its)\b)\S"
    r"|\brename\b[^\n]*?\bto\s+(?!(?:the|a|an|this|that|it|my|its)\b)\S"
    r"|\b(?:say|says|read|reads)\s+\S",
    re.I,
)
# "make it green (or should it be blue?)": the words in brackets are a second thought, not a
# second value; the parser would take the last colour.
_ALTERNATIVE = re.compile(r"\s*\(\s*or\b[^()]*\)", re.I)
# A sentence the parser took for a remark because it does not know its verb ("call the chart
# Sales", "order the table by Rank", "take the Year filter off this page") goes to the planner
# when it is said as a request and a tool card clearly fits it.
_PLAN_VERBS = (
    _EDIT_VERBS
    + r"|call|label|draw|let|order|restrict|limit|leave|take|trade|end|begin|start|include"
    r"|exclude|keep|lighten|darken|frame|mark|sync|group|ungroup|split|link|point|refresh"
    r"|widen|bring|send|name|title|filter"
)
_PLAN_START = re.compile(
    r"^(?:(?:please|ok(?:ay)?|now|also|then|and)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+(?:please\s+)?"
    r"|(?:i|we)\s+(?:want|would\s+like|'d\s+like)\s+(?:you\s+)?to\s+|let'?s\s+)?"
    rf"(?:{_PLAN_VERBS})\b",
    re.I,
)
_POLITE = re.compile(r"^(?:please\s+)?(?:can|could|would|will)\s+you\s+", re.I)
_WISH = re.compile(
    r"^(?:i|we)\s+(?:want|need|would\s+like|'d\s+like)\b|\bshould\s+(?:be|have|show|start|end)\b",
    re.I,
)
PROMOTE_SCORE = 3.0  # the best card's retrieval score a remark needs to reach the planner
RESCUE_SCORE = 2.0  # the same for a piece the parser took for an edit and could not read
# A question said as a request ("let me know which agent is top", "name the top agent",
# "could you show me the top 5 agents", "I want to know who ...", "title?", "include the table
# in your answer"): the question paths answer it; the planner never sees it.
_QUESTIONISH = re.compile(
    r"\b(?:give|offer|provide)\s+(?:(?:me|us)\s+)?(?:some\s+)?"
    r"(?:advice|recommendations?|suggestions?)\b|"
    r"\blet\s+me\s+know\b|\btell\s+(?:me|us)\b|\b(?:show|give|get|bring)\s+(?:me|us)\b|"
    r"\b(?:i|we)\s+(?:want|would\s+like|'d\s+like|need|wish)\s+to\s+(?:know|see|understand|"
    r"find\s+out|learn|hear)\b|\b(?:your|the)\s+(?:answer|reply|response|explanation|"
    r"comparison|analysis|summary)\b|"
    r"\bexplain\b|\bwho\b|\bwhat\b|\bwhich\b|\bhow\s+(?:many|much)\b|\bwhy\b|"
    # "name the top agent", "could you list the stores": naming data, not a report object
    r"^\s*(?:(?:please|can|could|would|will)\s+(?:you\s+)?)?(?:name|list|count|summari[sz]e|"
    r"compare|rank)\b(?!\s+(?:the\s+|this\s+|that\s+|a\s+)?(?:[\w'-]+\s+){0,3}?"
    r"(?:pages?|charts?|tables?|visuals?|slicers?|cards?|measures?|bookmarks?|columns?|"
    r"graphs?|matri(?:x|ces)|tabs?|it|them)\b)",
    re.I,
)
# A bare word or two with a question mark ("title?"): asking, not telling.
_BARE_QUESTION = re.compile(r"^\s*[\w' -]{1,30}\?\s*$")


def _questionish(text: str) -> bool:
    """Whether a piece asks for an answer rather than an edit (never planned)."""
    said = text.strip()
    if _BARE_QUESTION.match(said) and (
        len(said.split()) == 1 or not _PLAN_START.match(said.rstrip("?"))
    ):
        return True  # "title?", "which chart?"
    return _QUESTIONISH.search(said) is not None


def _a_tool_fits(text: str, context: EditContext, floor: float = PROMOTE_SCORE) -> bool:
    """Whether one of the best three tool cards for the words scores ``floor`` or more with
    one of its cue phrases."""
    from .edit_planner import available_cards, retrieve  # noqa: PLC0415 - costs a card read

    found = retrieve(text, context, available_cards(), limit=3)
    return any(s.score >= floor and s.cue > 0 for s in found)


def _asks_for_a_tool(text: str, context: EditContext) -> bool:
    """Whether a sentence the parser left as a remark is a request a tool card fits: said as a
    request (a verb first, a polite "could you ...", "I want ..."), not as a question, and the
    best card found for it scores well with at least one of its cue phrases."""
    said = text.strip()
    if _questionish(said) or (_ASKS.search(said) and not _POLITE.match(said)):
        return False
    if not (_PLAN_START.match(said) or _WISH.search(said)):
        return False
    return _a_tool_fits(said, context)


# Requests that start with a verb the parser does not take as one ("let the value axis begin at
# 0", "end the y axis at 50000", "draw a frame around the slicer").
_STYLING_START = re.compile(
    r"^\s*(?:please\s+)?(?:let|end|start|begin|draw)\b(?!\s+(?:me|us)\b)", re.I
)
# A remark with a subject ("I like the font", "the bars are blue") says something; one without
# ("bigger font", "dark theme please") asks for it.
_SUBJECT_START = re.compile(
    r"^\s*(?:i|we|you|they|he|she|it|this|that|these|those|my|our|the|its|their)\b", re.I
)


def _refuse_styling(result: ParseResult, context: EditContext) -> ParseResult:
    """Pieces the parser could not read, and remarks said as requests, that ask to change how
    the report looks: the styling refusal (Studio does not change styling), never the model
    tier or the question paths. A remark only when the remarks are the remainder as said."""
    parser = Parser(context)
    kept: list[str] = []
    refused: list[Clause] = []
    for piece in result.unparsed:
        found = None if _questionish(piece) else parser.styling(piece)
        if found is None:
            kept.append(piece)
        else:
            refused.append(found)
    idle = [c for c in result.clauses if c.kind is ParseKind.NOT_AN_EDIT and c.text.strip()]
    as_said = " ".join(c.text for c in idle).strip() == result.remainder.strip()
    clauses: list[Clause] = []
    left: list[str] = []
    turned = False
    for clause in result.clauses:
        said = clause.text.strip()
        request = (
            clause.kind is ParseKind.NOT_AN_EDIT
            and as_said
            and bool(said)
            and not _questionish(said)
            and bool(
                _PLAN_START.match(said)
                or _WISH.search(said)
                or _STYLING_START.match(said)
                or _edit_like(said)
                or not _SUBJECT_START.match(said)
            )
        )
        found = parser.styling(said) if request else None
        if found is not None:
            clauses.append(found)
            turned = True
            continue
        clauses.append(clause)
        if clause.kind is ParseKind.NOT_AN_EDIT and said:
            left.append(said)
    if not turned and not refused:
        return result
    return replace(
        result,
        clauses=(*clauses, *refused),
        unparsed=tuple(kept),
        remainder=" ".join(left) if as_said else result.remainder,
    )


def _to_planner(result: ParseResult, presentation: bool, context: EditContext) -> ParseResult:
    """The result with the remarks that are requests a tool fits moved to ``unparsed``. Only
    when the remarks are the whole remainder as said (a remainder the parser rewrote, "make a
    table of the top 5 agents" asking for figures, stays a question)."""
    if presentation:
        return result
    idle = [c for c in result.clauses if c.kind is ParseKind.NOT_AN_EDIT and c.text.strip()]
    if not idle or " ".join(c.text for c in idle).strip() != result.remainder.strip():
        return result
    moved = [c for c in idle if _asks_for_a_tool(c.text, context)]
    if not moved:
        return result
    return replace(
        result,
        clauses=tuple(c for c in result.clauses if c not in moved),
        remainder=" ".join(c.text for c in idle if c not in moved),
        unparsed=(*result.unparsed, *(c.text for c in moved)),
    )


def _edit_like(text: str) -> bool:
    return _EDIT_LIKE.search(text) is not None


def _read(message: str, context: EditContext) -> tuple[ParseResult, bool]:
    """The message as clauses, and whether it asks for figures phrased as an edit. A question
    tacked onto an edit with "and" ("make the bars green and what is the total sales?") is read
    apart: the edit parser takes the words after "and" as part of the edit and would lose the
    question, or ask which chart it was about. The other order ("what is the total sales, and
    make the bars green") is read apart the same way, or the edit would be dropped."""
    said = message.strip()
    found = _TRAILING_QUESTION.match(said)
    if found and not _TEXT_EDIT.search(found["edit"]):
        edit = parse_edit_message(found["edit"], context)
        if edit.unparsed or any(c.kind is not ParseKind.NOT_AN_EDIT for c in edit.clauses):
            asked = " ".join(p for p in (edit.remainder, found["question"]) if p)
            return replace(edit, remainder=asked), _rewritten(edit)
    first = _LEADING_QUESTION.match(said)
    if first and not _EDIT_LIKE.search(_ASKING.sub("", first["question"], count=1)):
        edit = parse_edit_message(first["edit"], context)
        if not edit.unparsed and any(c.kind is not ParseKind.NOT_AN_EDIT for c in edit.clauses):
            question = re.sub(r"^(?:first|also)\s+", "", first["question"], flags=re.I)
            asked = " ".join(p for p in (question, edit.remainder) if p)
            return replace(edit, remainder=asked), _rewritten(edit)
    result = parse_edit_message(message, context)
    return result, _rewritten(result)


@dataclass(frozen=True)
class Routed:
    """How the chat reads a message before anything is done."""

    result: ParseResult  # the clauses; ``unparsed`` is what goes to the planner
    presentation: bool  # "make a table of ..." asks for figures phrased as an edit
    said: str  # the words the clauses were read from (a second thought in brackets left out)
    note: str  # what the reply says about the words left out


def route_message(message: str, context: EditContext) -> Routed | None:
    """The message read the way the chat reads it (no model), or None when it asks and edits
    nothing, so the question paths answer it as they always did."""
    said = message
    alternatives = _ALTERNATIVE.findall(said)
    if alternatives and _ALTERNATIVE.sub("", said).strip():
        said = _ALTERNATIVE.sub("", said).strip()
    else:
        alternatives = []
    result, presentation = _read(said, context)
    if not presentation:
        result = _refuse_styling(result, context)
    # A piece the parser could not read reaches the model tier when it asks for a change (an
    # edit verb, a complaint) or a tool card clearly fits its words ("draw a frame around the
    # slicer", "order the table by Rank").
    fits = {
        u
        for u in result.unparsed
        if not _edit_like(u) and not _questionish(u) and _a_tool_fits(u, context, RESCUE_SCORE)
    }
    # a question said as a request never reaches the model tier ("name the top agent")
    kept = tuple(u for u in result.unparsed if (_edit_like(u) or u in fits) and not _questionish(u))
    if len(kept) != len(result.unparsed):
        # Statements that are no request ("I need this for the board meeting tomorrow") never
        # reach the model tier: they are the question paths' to answer.
        left = [u for u in result.unparsed if u not in kept]
        result = replace(
            result, unparsed=kept, remainder=" ".join(p for p in (result.remainder, *left) if p)
        )
    if (
        result.unparsed
        and all(c.kind is ParseKind.NOT_AN_EDIT for c in result.clauses)
        and _ASKS.search(result.remainder)
    ):
        # A question with a fragment the parser took for an edit ("i need a number before
        # 3pm: how many stores closed?"): it is a question, and reading it as an unreadable
        # edit would answer it with suggestion chips. A real unreadable edit sent together
        # with a question loses its edit here; it is rare, and the question is answered.
        return None
    result = _to_planner(result, presentation, context)
    actionable = bool(result.unparsed) or any(
        c.kind is not ParseKind.NOT_AN_EDIT for c in result.clauses
    )
    if not actionable and not presentation:
        return None
    note = (
        "I left out the part in brackets: " + " ".join(a.strip() for a in alternatives) + "."
        if alternatives
        else ""
    )
    return Routed(result, presentation, said, note)


_TYPED_YES = re.compile(
    r"^(?:(?:yes|yeah|yep|yup|sure|ok(?:ay)?|go\s+ahead|do\s+it|apply(?:\s+it)?|confirm(?:ed)?|"
    r"please\s+do|proceed)(?:[\s,.!]+|$))+"
    r"(?:please|thanks?|thank\s+you)?[\s.!]*$",
    re.I,
)
_TYPED_NO = re.compile(
    r"^(?:(?:no|nope|cancel|don'?t|do\s+not|stop|never\s*mind|leave\s+it)(?:[\s,.!]+|$))+"
    r"(?:please|thanks?|thank\s+you)?[\s.!]*$",
    re.I,
)


def typed_answer(message: str) -> bool | None:
    """True for a short yes ("yes, go ahead", "ok", "apply"), False for a short no ("no",
    "cancel", "don't"), None for anything longer or not clearly one of them (a new message)."""
    said = " ".join(message.strip().split())
    if not said or len(said) > 40:
        return None
    if _TYPED_YES.match(said):
        return True
    if _TYPED_NO.match(said):
        return False
    return None


def _typed_arrow_count(message: str, pending: Pending | None) -> int | None:
    """A bare count answers only the existing grounded colored-arrow count menu."""
    counts = {
        "three": "ThreeColoredArrows",
        "3": "ThreeColoredArrows",
        "four": "FourColoredArrows",
        "4": "FourColoredArrows",
        "five": "FiveColoredArrows",
        "5": "FiveColoredArrows",
    }
    wanted = counts.get(message.strip().casefold())
    if wanted is None or pending is None or pending.kind != "clarify" or pending.clause is None:
        return None
    choices = pending.clause.choices
    expected = set(counts.values())
    if len(choices) != 3 or {choice.label for choice in choices} != expected:
        return None
    base = None
    for choice in choices:
        if len(choice.ops) != 1:
            return None
        operation = choice.ops[0]
        if (
            operation.get("op") != "set_conditional_format"
            or operation.get("style") != "icons"
            or operation.get("icon_set") != choice.label
            or not operation.get("field")
            or not operation.get("visual")
        ):
            return None
        shared = {key: value for key, value in operation.items() if key != "icon_set"}
        if base is not None and shared != base:
            return None
        base = shared
    return next(index for index, choice in enumerate(choices) if choice.label == wanted)


def imported_edit_events(
    port: EditPort,
    request: ChatRequest,
    report: ImportedReport | None,
    snapshot: DatasetSnapshot | None,
    cancel: threading.Event,
    *,
    env: ChatEditEnv,
) -> Iterator[Event] | None:
    """The events of a chat message that edits an imported report, or None when the message
    asks and edits nothing (the caller answers it as it always did).

    The message is read here, on the caller's thread and with no model, so the answer to
    "is this an edit?" is immediate; carrying it out runs on its own thread.

    A short "yes" / "no" typed while the chat's last question is a confirmation (a model
    change, a plan) answers it, exactly as its Apply or Cancel button would.
    A bare arrow count answers only the live, grounded colored-arrow count menu.
    """
    verdict = typed_answer(request.message)
    latest = env.pending.latest(request.workspace_id)
    index = _typed_arrow_count(request.message, latest)
    if index is not None and latest is not None:
        try:
            current = port.head(request.workspace_id)
        except Exception:  # noqa: BLE001 - a failed read must not apply a stale choice
            current = {}
        if latest.head is not None and current.get("id") == latest.head:
            key = request.idempotency_key
            return imported_edit_answer_events(
                port,
                request.workspace_id,
                ChatEditChoiceRequest(
                    pending_id=latest.id,
                    index=index,
                    idempotency_key=key if key and len(key) >= 8 else None,
                ),
                report,
                snapshot,
                cancel,
                env=env,
                user_text=request.message.strip(),
            )
    if verdict is not None and latest is not None and latest.kind == "confirm":
        key = request.idempotency_key
        answer = ChatEditConfirmRequest(
            pending_id=latest.id,
            accept=verdict,
            idempotency_key=key if key and len(key) >= 8 else None,
        )
        return imported_edit_answer_events(
            port,
            request.workspace_id,
            answer,
            report,
            snapshot,
            cancel,
            env=env,
            user_text=request.message.strip(),
        )
    # anything else is a new message: a later "yes" no longer answers the question before it
    env.pending.forget_latest(request.workspace_id)
    scope = request.context
    selection = scope.visual_id if scope else None
    page = scope.page_id if scope else None
    try:
        context = port.edit_context(request.workspace_id, selection, page)
    except Exception as error:  # noqa: BLE001 - a report that cannot be read must not stop questions
        env.log("chat.edit_context_failed", {"reason": str(error)[:200]})
        return None
    routed = route_message(request.message, context)
    if routed is None:
        return None
    result, presentation, said, note = routed.result, routed.presentation, routed.said, routed.note

    def produce(emit: Callable[[Event], None]) -> None:
        _Turn(
            port=port,
            env=env,
            request=request,
            workspace_id=request.workspace_id,
            user_text=request.message,
            key=request.idempotency_key or uuid.uuid4().hex,
            cancel=cancel,
            emit=emit,
            context=context,
            report=report,
            snapshot=snapshot,
            selection=selection,
            page=page,
            said=said,
        ).run(result, presentation, note)

    return _spawn(produce, cancel)


def imported_edit_answer_events(
    port: EditPort,
    workspace_id: str,
    request: ChatEditChoiceRequest | ChatEditConfirmRequest,
    report: ImportedReport | None,
    snapshot: DatasetSnapshot | None,
    cancel: threading.Event,
    *,
    env: ChatEditEnv,
    user_text: str | None = None,
) -> Iterator[Event]:
    """The events of the answer to an ``edit_choices`` (a choice) or ``edit_confirm`` question
    (``user_text``: the words typed instead of a click, as the history shows them)."""
    key = request.idempotency_key or uuid.uuid4().hex
    flight: Flight | None = None
    if request.idempotency_key:
        # A retry of the same answer while the first is still being carried out (the client
        # dropped and asked again) joins it: the question is taken by the first request, so
        # a second turn would find nothing, say "expired" and have that recorded under the key.
        mine = (
            request.pending_id,
            getattr(request, "accept", None),
            getattr(request, "index", None),
        )
        flight, first = env.pending.begin(workspace_id, key, mine)
        if not first:
            return flight.follow(cancel)

    def produce(emit: Callable[[Event], None]) -> None:
        def relay(item: Event) -> None:
            if flight is not None:
                flight.add(item)
            emit(item)

        try:
            pending = env.pending.take(workspace_id, request.pending_id)
            label = user_text or (
                "An answer to an earlier question"
                if pending is None
                else _answer_label(pending, request)
            )
            context = port.edit_context(
                workspace_id,
                pending.selection if pending else None,
                pending.page if pending else None,
            )
            turn = _Turn(
                port=port,
                env=env,
                request=request,
                workspace_id=workspace_id,
                user_text=label,
                key=key,
                cancel=cancel,
                emit=relay,
                context=context,
                report=report,
                snapshot=snapshot,
                selection=pending.selection if pending else None,
                page=pending.page if pending else None,
            )
            if pending is None:
                turn.plain(EXPIRED, code="expired")
            else:
                turn.guarded(lambda: _answer(turn, pending, request, env))
            turn.finish()
        finally:
            if flight is not None:
                flight.finish()
                env.pending.end(workspace_id, key, flight)

    return _spawn(produce, cancel)


def _answer_label(pending: Pending, request: ChatEditChoiceRequest | ChatEditConfirmRequest) -> str:
    """What the user's click is called in the chat history."""
    if isinstance(request, ChatEditConfirmRequest):
        return "Yes, go ahead." if request.accept else "No, leave it."
    if (
        pending.kind == "clarify"
        and pending.clause is not None
        and 0 <= request.index < len(pending.clause.choices)
    ):
        return pending.clause.choices[request.index].label
    if pending.kind == "options" and 0 <= request.index < len(pending.options):
        return pending.options[request.index].label
    return "An answer to an earlier question"


def _answer(
    turn: _Turn,
    pending: Pending,
    request: ChatEditChoiceRequest | ChatEditConfirmRequest,
    env: ChatEditEnv,
) -> None:
    """Carry out what a click answers. A question that cannot be answered as sent (the wrong
    kind of button, an index that is not there) or whose answer met a busy report stays open."""
    problem = _answer_problem(pending, request)
    if problem:
        env.pending.put_back(pending)
        turn.plain(problem)
        return
    if isinstance(request, ChatEditConfirmRequest):
        if not request.accept:
            turn.plain(_LEFT_AS_IT_WAS)
            return
        assert pending.action is not None
        turn.head = pending.head  # the operations were worked out against that head
        turn.execute(pending.action)
    elif pending.kind == "options":
        action = pending.options[request.index].action
        if action is None:
            turn.plain("Okay, I left them as they are.")
            return
        turn.head = pending.head
        turn.execute(action)
    else:
        assert pending.clause is not None
        choice = pending.clause.choices[request.index]
        try:
            resolved = apply_choice(pending.clause, request.index, turn.context)
        except IndexError:
            env.pending.put_back(pending)
            turn.plain("That choice is not available.")
            return
        # A ready-made choice carries values worked out when it was offered; a slot answer was
        # read again just now against the head as it is.
        turn.head = pending.head if choice.ops else turn.context.head_revision
        turn.process([resolved])
    if any(
        e["event"] == "edit_failed" and e["data"].get("code") in {"edit_busy", "desktop_busy"}
        for e in turn.saved
    ):
        env.pending.put_back(pending)


def _answer_problem(
    pending: Pending, request: ChatEditChoiceRequest | ChatEditConfirmRequest
) -> str:
    """Why this answer does not fit its question, or an empty string when it does."""
    if isinstance(request, ChatEditConfirmRequest):
        if pending.kind != "confirm" or pending.action is None:
            return "That question needs one of its answer buttons, not Yes or No."
        return ""
    if pending.kind == "confirm":
        return "That question needs Yes or No, not a choice."
    count = (
        len(pending.options)
        if pending.kind == "options"
        else len(pending.clause.choices if pending.clause is not None else ())
    )
    return "" if 0 <= request.index < count else "That choice is not available."


def replay_events(prior: dict[str, Any]) -> Iterator[Event]:
    """The events of a turn recorded earlier under the same idempotency key: the edit events,
    then the text."""
    yield from prior.get("edit_events") or ()
    yield _token(str(prior.get("text", "")))


def collect_answer(events: Iterable[Event]) -> tuple[int, dict[str, Any]]:
    """The events of an answer as one JSON body: ``{message, text, receipt, events}`` (HTTP 200),
    or, when the change failed or the question had expired, ``{code, message, detail, ...}`` with
    the error's status. For a client that does not read the event stream."""
    text: list[str] = []
    kept: list[Event] = []
    receipt: Any = None
    failure: dict[str, Any] | None = None
    for item in events:
        name, data = item["event"], item["data"]
        if name == "token":
            text.append(str(data.get("text", "")))
            continue
        if name == "edit_progress":
            continue
        kept.append(item)
        if name == "edit_applied":
            receipt = data.get("receipt")
        elif name == "edit_failed":
            failure = data
    message = "".join(text).strip()
    body: dict[str, Any] = {"message": message, "text": message, "receipt": receipt, "events": kept}
    if failure is not None and receipt is None:
        code = str(failure.get("code", "edit_rejected"))
        text_of = str(failure.get("message", message))
        body |= {"code": code, "message": text_of, "detail": text_of}
        return _STATUS.get(code, 422), body
    return 200, body
