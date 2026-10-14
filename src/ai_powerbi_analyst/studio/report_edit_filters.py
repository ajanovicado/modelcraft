"""Filter operations in the edit layer: the handlers of ``SetPageFilter``, ``SetVisualFilter``,
``SetReportFilter`` and ``ClearFilter``.

The operation classes and the way a filter is written live in ``report_filters``; this module
is what ``apply_operations`` calls. It adds what a batch needs on top:

- one set of the report's filter names for the whole batch (a filter name is unique across the
  report, bookmarks included), read once at the first filter operation and kept up to date as
  filters are added, so two filters set in one batch never share a name;
- every operation goes through ``spec_variants``, so a shape that ``VERIFIED_VARIANTS`` turns
  off is refused here, in one place, instead of being written;
- a warning when Studio's canvas cannot draw the filter written (the visuals it covers say
  "Needs Power BI" there; Power BI itself applies it).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .pbir_document import DocumentError, PbirDocument
from .report_edit_context import Handler, HandlerResult, ModelInfo
from .report_filters import (
    FILTER_OPERATIONS,
    VERIFIED_VARIANTS,
    ClearFilter,
    FilterOperation,
    SetPageFilter,
    SetReportFilter,
    SetVisualFilter,
    apply_filter,
    collect_filter_names,
    filter_unsupported_reason,
    spec_variants,
)
from .report_filters import FieldRef as FilterFieldRef

# The ``kind`` labels of a filter specification: they appear in an error's location the way a
# union form's name does, and mean nothing to a user.
FILTER_SPEC_KINDS = frozenset({"categorical", "advanced", "top_n", "relative_date"})

__all__ = [
    "FILTER_HANDLERS",
    "FILTER_OPERATIONS",
    "FILTER_SPEC_KINDS",
    "FilterFieldRef",
    "FilterOperation",
    "ClearFilter",
    "SetPageFilter",
    "SetReportFilter",
    "SetVisualFilter",
    "filter_batch",
]


class _BatchNames:
    def __init__(self, document: PbirDocument) -> None:
        self.document = document
        self._names: set[str] | None = None

    def get(self) -> set[str]:
        if self._names is None:
            self._names = collect_filter_names(self.document)
        return self._names


_ACTIVE: ContextVar[_BatchNames | None] = ContextVar("report_edit_filter_names", default=None)


@contextmanager
def filter_batch(document: PbirDocument) -> Iterator[None]:
    """Share one set of filter names between the filter operations run inside the block."""
    token = _ACTIVE.set(_BatchNames(document))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _batch_names(document: PbirDocument) -> set[str]:
    active = _ACTIVE.get()
    if active is not None and active.document is document:
        return active.get()
    return collect_filter_names(document)


def _refuse_unverified(
    op: SetPageFilter | SetVisualFilter | SetReportFilter, model: ModelInfo
) -> None:
    keys = spec_variants(op.filter, model, locked=op.locked, hidden=op.hidden)
    off = [k for k in keys if not VERIFIED_VARIANTS.get(k, False)]
    if off:
        words = ", ".join(k.replace("_", " ") for k in off)
        raise DocumentError(
            f"Studio has not yet checked in Power BI how to write this kind of filter ({words}), "
            "so it will not add it. Add it in Power BI."
        )


def _apply_filter(document: PbirDocument, op: FilterOperation, model: ModelInfo) -> HandlerResult:
    if not isinstance(op, ClearFilter):
        _refuse_unverified(op, model)
    applied = apply_filter(document, op, model, _batch_names(document))
    warnings: list[str] = []
    reason = filter_unsupported_reason(applied.filter) if applied.filter is not None else None
    if reason:
        warnings.append(
            "Studio's canvas cannot draw this filter; the affected visuals will say Needs Power "
            f"BI, and Power BI applies it. {reason}."
        )
    return HandlerResult(applied.summary, warnings=warnings)


FILTER_HANDLERS: dict[str, Handler] = {
    "set_page_filter": _apply_filter,
    "set_visual_filter": _apply_filter,
    "set_report_filter": _apply_filter,
    "clear_filter": _apply_filter,
}
