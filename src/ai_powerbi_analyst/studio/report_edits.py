"""Typed edits to an imported report, applied the way Power BI Desktop writes them.

Each operation is a closed model. ``apply_operations`` applies a list in order to a
PbirDocument and returns one plain-English line per change. Report-layer operations
edit PBIR JSON, which Desktop opens as written; the evidence is in
docs/imported-report-editing.md. Model-layer operations (measures, and the ``model`` family's
columns, tables, relationships, hierarchies, field parameters, calculation groups, roles and
Power Query) are validated here and applied through Power BI Desktop, because the DataModel is a
binary Desktop owns.

Every property write is checked against Microsoft's formatting catalog for the visual's
type (pbir_catalog.json), and existing entries are merged, never replaced. pbi-cli's
set-container lost a title's font, size and wrap that way.

Studio does not change styling: the operations that set colors, fonts, titles, data labels,
legends, axes, a visual's box, page backgrounds and the theme (the former ``format`` family, and
the page family's background and theme) were removed, and chat refuses such a request
(``edit_capabilities``). The renderer still draws every report's own formatting.

This module is the facade over the family modules; every name importable from it before the
split still is. Where things live:

- ``report_edit_ops``: the operation classes and the values they share (colors, field refs).
- ``report_edit_context``: ``ModelInfo``, ``EditOutcome`` and the visual helpers all families use.
- ``report_edit_layout``, ``report_edit_pages``,
  ``report_edit_measures``, ``report_edit_filters``, ``report_edit_conditional``,
  ``report_edit_navigation``, ``report_edit_extras``, ``report_edit_model``: one operation
  family each, with its handlers (the filter classes themselves live in ``report_filters``).
- ``report_edit_validation``: ``validate_document``, and ``validate_findings`` with
  ``capture_baseline`` for holding an edit only to the problems it introduced.

``apply_operations`` applies a whole batch; ``apply_report_operations`` applies only the
report-layer part of one that has model operations, on the file Desktop saved after applying
them.

Adding an operation touches its family module and its marked block here. Define the class next
to its handler, add it to the family's ``*Operation`` union (``FormatOperation``, ...) and its
handler to the family's ``*_HANDLERS`` dict. ``ReportOperation`` below is the union of those
family unions, so ``EditBatch`` and ``parse_operations`` accept the new operation with no
change to the union. What this file needs is a re-export: import the new public names in the
family's marked import block and list them in its marked ``__all__`` block (mypy --strict
treats a name as re-exported only when it is in ``__all__``). A new family joins
``ReportOperation``, ``REPORT_OPERATIONS`` and ``_HANDLERS`` the way ``filters`` did.

The union is built from the families' unions, not from their runtime tuples, because
mypy --strict must see every member statically. ``LAYOUT_OPERATIONS`` and its siblings are
derived from the same unions, and a test checks that the union, the tuples and the handlers
agree.
"""

from __future__ import annotations

import contextlib
import difflib
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Any

from pydantic import Field, TypeAdapter, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel

from . import pbir_expr as expr
from .pbir_document import ROOT, DocumentError, PbirDocument

# --- shared: the values, the model facts and the helpers every family uses -------------
from .report_edit_context import (
    _DISPLAY_UNITS,
    Choice,
    EditOutcome,
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    Protected,
    VisibleIn,
    _category_selector,
    _field_projection,
    _has,
    _label,
    _objects,
    _page_bounds,
    _projections,
    _query_state,
    _series_selector,
    _set,
    _visual_op,
    _visual_type,
    _z_values,
    page_copy_lineage,
    visible_caption,
)
from .report_edit_ops import (
    AddPage,
    AddVisual,
    Arrange,
    ChangeVisualType,
    ColorValue,
    CreateMeasure,
    DeleteMeasure,
    DeletePage,
    DeleteVisual,
    DuplicatePage,
    DuplicateVisual,
    FieldRef,
    HexColor,
    MovePage,
    RenameMeasure,
    RenamePage,
    SetFields,
    SetPageHidden,
    SetPageSize,
    SetPosition,
    SetSort,
    SetVisualHidden,
    ThemeColorRef,
    UpdateMeasure,
    _color_arg,
)

# --- FAMILY: layout (owner: layout operations) ----------------------------------------------
# isort: split
from .report_edit_align import (
    ALIGN_HANDLERS,
    ALIGN_OPERATIONS,
    AddShape,
    AddTextbox,
    AlignOperation,
    AlignVisuals,
    DistributeVisuals,
    PlaceVisual,
    SwapVisuals,
)
from .report_edit_layout import (
    ADDABLE_TYPES,
    LAYOUT_HANDLERS,
    LAYOUT_OPERATIONS,
    LayoutOperation,
    _apply_add_visual,
    _apply_arrange,
    _apply_change_visual_type,
    _apply_delete_visual,
    _apply_duplicate_visual,
    _apply_set_fields,
    _apply_set_position,
    _apply_set_sort,
    _apply_set_visual_hidden,
    _assign_roles,
    _drop_selectors,
)

# --- FAMILY: pages (owner: page operations) ---------------------------------------
# isort: split
from .report_edit_pages import (
    PAGE_HANDLERS,
    PAGE_OPERATIONS,
    PageOperation,
    _apply_add_page,
    _apply_delete_page,
    _apply_duplicate_page,
    _apply_move_page,
    _apply_rename_page,
    _apply_set_page_hidden,
    _apply_set_page_size,
    _insert_page,
    _new_page,
)

# --- FAMILY: measures (owner: model-layer measure operations) -------------------------------
# isort: split
from .report_edit_measures import (
    MEASURE_HANDLERS,
    MEASURE_OPERATIONS,
    MEASURE_REPORT_HANDLERS,
    MeasureOperation,
    _check_create_measure,
    _check_delete_measure,
    _check_rename_measure,
    _check_update_measure,
    _rename_references,
    _uses_measure,
)

# --- FAMILY: validation (owner: document validation) ----------------------------------------
# isort: split
from .report_edit_validation import (
    Baseline,
    Finding,
    capture_baseline,
    validate_document,
    validate_findings,
)

# --- FAMILY: filters (owner: filter operations) ---------------------------------------------
# isort: split
from .report_edit_filters import (
    FILTER_HANDLERS,
    FILTER_OPERATIONS,
    FILTER_SPEC_KINDS,
    ClearFilter,
    FilterFieldRef,
    FilterOperation,
    SetPageFilter,
    SetReportFilter,
    SetVisualFilter,
    filter_batch,
)

# --- FAMILY: conditional (owner: conditional formatting, number formats, table layout) --------
# isort: split
from .report_edit_conditional import (
    CONDITIONAL_HANDLERS,
    CONDITIONAL_OPERATIONS,
    ConditionalOperation,
)

# --- FAMILY: navigation (owner: bookmarks, buttons, drillthrough, interactions, sync) ---------
# isort: split
from .report_edit_navigation import (
    NAVIGATION_HANDLERS,
    NAVIGATION_OPERATIONS,
    NavigationOperation,
)

# --- FAMILY: extras (owner: analytics, mobile, groups, visual calculations, painter...) --------
# isort: split
from .report_edit_extras import EXTRAS_HANDLERS, EXTRAS_OPERATIONS, ExtrasOperation

# --- FAMILY: model (owner: the model beyond measures, applied through Power BI Desktop) --------
# isort: split
from .report_edit_model import (
    MODEL_FAMILY_OPERATIONS,
    MODEL_HANDLERS,
    MODEL_REPORT_HANDLERS,
    ModelFamilyOperation,
)

# --- the operations ---------------------------------------------------------------------------

# One class per family module's union; a new family is one more term. Every term must be a
# union (or class) of operations whose ``op`` is a distinct Literal, for the discriminator.
ReportOperation = Annotated[
    LayoutOperation
    | PageOperation
    | MeasureOperation
    | FilterOperation
    | ConditionalOperation
    | NavigationOperation
    | ExtrasOperation
    | ModelFamilyOperation,
    Field(discriminator="op"),
]

# The same classes as runtime tuples, one per family.
REPORT_OPERATIONS: tuple[type[ClosedModel], ...] = (
    *LAYOUT_OPERATIONS,
    *PAGE_OPERATIONS,
    *MEASURE_OPERATIONS,
    *FILTER_OPERATIONS,
    *CONDITIONAL_OPERATIONS,
    *NAVIGATION_OPERATIONS,
    *EXTRAS_OPERATIONS,
    *MODEL_FAMILY_OPERATIONS,
)
# Operations applied through Power BI Desktop: the model layer. The editor sends every
# operation of these classes to Desktop (measures through ``pbi measure`` alone, anything else
# through Studio's TOM runner).
MODEL_OPERATIONS: tuple[type[ClosedModel], ...] = (*MEASURE_OPERATIONS, *MODEL_FAMILY_OPERATIONS)
# What a model operation does to the report once Desktop has applied its model half: only the
# renames (of a measure, a column or a table) rewrite the report's references.
REPORT_HALF_HANDLERS: dict[str, Handler] = {**MEASURE_REPORT_HANDLERS, **MODEL_REPORT_HANDLERS}


class EditBatch(ClosedModel):
    operations: list[ReportOperation] = Field(min_length=1, max_length=50)


_OPERATION_ADAPTER: TypeAdapter[Any] = TypeAdapter(ReportOperation)
_MAX_STEPS = 50
# Pydantic reports a union member that is a model as a type error too; it says nothing a
# user can act on when another error names the field.
_SHAPE_ERRORS = {"model_type", "model_attributes_type", "dict_type"}


def parse_operations(raw: Sequence[Mapping[str, Any]]) -> list[ReportOperation]:
    """Operations as dicts (from chat or the HTTP API) into typed operations.

    The rules are ``EditBatch``'s: one to 50 steps, each validating into exactly one
    operation class. A step that does not, raises DocumentError naming it ("Step 2 ...") in
    plain English."""
    if not raw:
        raise DocumentError("Say what to change: the batch has no steps")
    if len(raw) > _MAX_STEPS:
        raise DocumentError(f"A batch holds at most {_MAX_STEPS} steps; this one has {len(raw)}")
    parsed: list[ReportOperation] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise DocumentError(f"Step {index} is not an operation: expected a set of settings")
        try:
            parsed.append(_OPERATION_ADAPTER.validate_python(item))
        except ValidationError as error:
            raise DocumentError(_describe_step(index, item, error)) from error
    return parsed


def _describe_step(index: int, item: Mapping[str, Any], error: ValidationError) -> str:
    """One plain-English sentence for the first thing wrong with a step."""
    problems = error.errors(include_url=False, include_context=False, include_input=False)
    first = problems[0]
    # The tag of the whole step is 'op'; a nested union (a filter's kind) has a longer location
    # and is reported like any other field below.
    if first["type"] == "union_tag_not_found" and not first["loc"]:
        return f"Step {index} does not say what to do (it has no 'op')"
    if first["type"] == "union_tag_invalid" and not first["loc"]:
        name = str(item.get("op"))
        known = sorted(cls.model_fields["op"].default for cls in REPORT_OPERATIONS)
        close = difflib.get_close_matches(name, known, n=1)
        hint = f"; did you mean {close[0]!r}?" if close else ""
        return f"Step {index}: {name!r} is not an operation Studio knows{hint}"
    # loc is (operation name, field, ...). A field that takes several forms (a color is a hex
    # string or a theme reference) reports one error per form: the deepest one names the most.
    field_problems = [p for p in problems if len(p["loc"]) > 1 and p["type"] not in _SHAPE_ERRORS]
    chosen = max(field_problems or problems, key=lambda p: len(p["loc"]))
    where = f"Step {index} ({item.get('op')})"
    if len(chosen["loc"]) < 2:
        return f"{where}: {chosen['msg'].rstrip('.')}"
    subject = repr(str(chosen["loc"][1]))
    # Positions inside the field: a list item, or a setting of a nested value. Pydantic also
    # puts the union form's label ("constrained-str", "ThemeColorRef") in the path; a setting
    # name is lower snake case.
    # A filter's kind ("categorical"...) is the label of the union form the same way.
    inside = [
        f"item {part + 1}" if isinstance(part, int) else str(part)
        for part in chosen["loc"][2:]
        if (isinstance(part, int) or re.fullmatch(r"[a-z][a-z0-9_]*", str(part)))
        and part not in FILTER_SPEC_KINDS
    ]
    if inside:
        subject += f" ({', '.join(inside)})"
    if chosen["type"] == "missing":
        return f"{where} needs {subject}"
    if chosen["type"] == "extra_forbidden":
        return f"{where} has no setting called {subject}"
    if chosen["type"] == "string_pattern_mismatch":
        return f"{where}: {subject} is not in the expected format (colors look like #1E90FF)"
    return f"{where}: {subject} is not valid: {chosen['msg'][:1].lower()}{chosen['msg'][1:]}"


# --- operation handlers ------------------------------------------------------------------------

_HANDLERS: dict[str, Handler] = {
    **LAYOUT_HANDLERS,
    **PAGE_HANDLERS,
    **MEASURE_HANDLERS,
    **FILTER_HANDLERS,
    **CONDITIONAL_HANDLERS,
    **NAVIGATION_HANDLERS,
    **EXTRAS_HANDLERS,
    **MODEL_HANDLERS,
}


def _run_operations(
    document: PbirDocument, operations: list[Any], model: ModelInfo, *, report_layer_only: bool
) -> EditOutcome:
    outcome = EditOutcome()
    # The filter operations of one batch share the report's filter names (unique report-wide).
    with filter_batch(document):
        for index, operation in enumerate(operations):
            is_model_op = isinstance(operation, MODEL_OPERATIONS)
            handler: Any = _HANDLERS[operation.op]
            if report_layer_only and is_model_op:
                # The model half ran in Desktop already; only what the operation does to the
                # report itself (a rename's references) is left, and most have none.
                handler = REPORT_HALF_HANDLERS.get(operation.op)
                if handler is None:
                    continue
            copying = None
            if operation.op == "duplicate_page":
                with contextlib.suppress(DocumentError):  # the handler says why
                    copying = document.resolve_page(operation.page)
            pages_before = list(document.pages)
            try:
                result = handler(document, operation, model)
            except expr.ExpressionError as error:
                raise DocumentError(str(error)) from error
            except (NeedsConfirmation, NeedsChoice) as error:
                # The caller acts on the type (confirm, ask), so it keeps its class and payload.
                error.step = index + 1
                raise
            except DocumentError as error:
                prefix = f"Step {index + 1}: " if len(operations) > 1 else ""
                raise DocumentError(prefix + str(error)) from error
            # The operation comes before what its handler adds after it: a rename's
            # dependents can only be updated once the new name exists.
            if is_model_op and not report_layer_only:
                outcome.model_operations.append(operation)
            if isinstance(result, HandlerResult):
                outcome.summaries.append(result.summary)
                outcome.warnings.extend(result.warnings)
                outcome.protected.extend(result.protected)
                outcome.replaced.extend(result.replaced)
                outcome.visible_in.extend(result.visible_in)
                if not report_layer_only:
                    outcome.model_operations.extend(result.model_operations)
                outcome.pending_model_operations.extend(result.pending_model_operations)
                outcome.lineage.update(result.lineage)
                outcome.partial = outcome.partial or result.partial
            else:
                outcome.summaries.append(result)
            if copying is not None and not (isinstance(result, HandlerResult) and result.lineage):
                # A page copy starts with its source's defects; the pages handler does not say
                # which visual came from which, so pair them here.
                outcome.lineage.update(page_copy_lineage(document, copying, pages_before))
            # A model operation changes the report only through its report half (a rename).
            if not is_model_op or operation.op in REPORT_HALF_HANDLERS:
                outcome.report_changed = True
    return outcome


def apply_operations(
    document: PbirDocument, operations: list[Any], model: ModelInfo
) -> EditOutcome:
    """Apply every operation or none: the first failure raises DocumentError and the
    caller discards the document (nothing is written until ``document.commit``).

    ``model`` is the model before the batch. ``EditOutcome.model_operations`` lists what
    Desktop has to apply, in order: each model operation followed by the operations its
    handler adds (the dependents of a renamed measure)."""
    return _run_operations(document, operations, model, report_layer_only=False)


def apply_report_operations(
    document: PbirDocument, operations: list[Any], model: ModelInfo
) -> EditOutcome:
    """Apply the report-layer part of a batch that has model operations: every ordinary
    operation, and of a model operation only what it does to the report (a rename of a measure,
    column or table rewrites the references; the others do nothing there). This is the second
    stage of a batch, on the file Desktop saved after applying the model operations, so
    ``model`` is the model after them and ``outcome.model_operations`` stays empty."""
    return _run_operations(document, operations, model, report_layer_only=True)


__all__ = [
    "ROOT",
    "Choice",
    "DocumentError",
    "EditBatch",
    "EditOutcome",
    "FieldRef",
    "HandlerResult",
    "ModelInfo",
    "NeedsChoice",
    "NeedsConfirmation",
    "Protected",
    "VisibleIn",
    "ReportOperation",
    "apply_operations",
    "apply_report_operations",
    "parse_operations",
    "validate_document",
]

# The names below are re-exports of what this module offered before it was split into family
# modules; other modules and tests import them from here. Each family's block lists its own.
# --- shared ---
__all__ += [
    "_DISPLAY_UNITS",
    "ColorValue",
    "Handler",
    "HexColor",
    "ThemeColorRef",
    "_category_selector",
    "_color_arg",
    "_field_projection",
    "_has",
    "_label",
    "_objects",
    "_page_bounds",
    "_projections",
    "_query_state",
    "_series_selector",
    "_set",
    "_visual_op",
    "_visual_type",
    "_z_values",
    "visible_caption",
]
# --- FAMILY: layout ---
__all__ += [
    "ADDABLE_TYPES",
    "ALIGN_HANDLERS",
    "ALIGN_OPERATIONS",
    "LAYOUT_HANDLERS",
    "LAYOUT_OPERATIONS",
    "AddShape",
    "AddTextbox",
    "AddVisual",
    "AlignOperation",
    "AlignVisuals",
    "Arrange",
    "ChangeVisualType",
    "DeleteVisual",
    "DistributeVisuals",
    "DuplicateVisual",
    "LayoutOperation",
    "PlaceVisual",
    "SwapVisuals",
    "SetFields",
    "SetPosition",
    "SetSort",
    "SetVisualHidden",
    "_apply_add_visual",
    "_apply_arrange",
    "_apply_change_visual_type",
    "_apply_delete_visual",
    "_apply_duplicate_visual",
    "_apply_set_fields",
    "_apply_set_position",
    "_apply_set_sort",
    "_apply_set_visual_hidden",
    "_assign_roles",
    "_drop_selectors",
]
# --- FAMILY: pages ---
__all__ += [
    "PAGE_HANDLERS",
    "PAGE_OPERATIONS",
    "AddPage",
    "DeletePage",
    "DuplicatePage",
    "MovePage",
    "PageOperation",
    "RenamePage",
    "SetPageHidden",
    "SetPageSize",
    "_apply_add_page",
    "_apply_delete_page",
    "_apply_duplicate_page",
    "_apply_move_page",
    "_apply_rename_page",
    "_apply_set_page_hidden",
    "_apply_set_page_size",
    "_insert_page",
    "_new_page",
]
# --- FAMILY: measures ---
__all__ += [
    "MEASURE_HANDLERS",
    "MEASURE_OPERATIONS",
    "MEASURE_REPORT_HANDLERS",
    "MODEL_OPERATIONS",
    "CreateMeasure",
    "DeleteMeasure",
    "MeasureOperation",
    "RenameMeasure",
    "UpdateMeasure",
    "_check_create_measure",
    "_check_delete_measure",
    "_check_rename_measure",
    "_check_update_measure",
    "_rename_references",
    "_uses_measure",
]
# --- FAMILY: validation ---
# (validate_document is in the public list above.)
__all__ += ["Baseline", "Finding", "capture_baseline", "validate_findings"]
# --- FAMILY: filters ---
__all__ += [
    "FILTER_HANDLERS",
    "FILTER_OPERATIONS",
    "FILTER_SPEC_KINDS",
    "ClearFilter",
    "FilterFieldRef",
    "FilterOperation",
    "SetPageFilter",
    "SetReportFilter",
    "SetVisualFilter",
    "filter_batch",
]
# --- FAMILY: conditional ---
__all__ += ["CONDITIONAL_HANDLERS", "CONDITIONAL_OPERATIONS", "ConditionalOperation"]
# --- FAMILY: navigation ---
__all__ += ["NAVIGATION_HANDLERS", "NAVIGATION_OPERATIONS", "NavigationOperation"]
# --- FAMILY: extras ---
__all__ += ["EXTRAS_HANDLERS", "EXTRAS_OPERATIONS", "ExtrasOperation"]
# --- FAMILY: model ---
__all__ += [
    "MODEL_FAMILY_OPERATIONS",
    "MODEL_HANDLERS",
    "MODEL_REPORT_HANDLERS",
    "REPORT_HALF_HANDLERS",
    "ModelFamilyOperation",
]
