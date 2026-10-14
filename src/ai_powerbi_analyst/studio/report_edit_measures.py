"""Model-layer measure operations: create, update, rename and delete.

Report edits change PBIR JSON, which Desktop opens as written. A measure lives in the data
model, a binary only Desktop can change, so these handlers check the operation against
``ModelInfo`` (and, for rename and delete, against every place the report and the model's own
DAX refer to the measure) and ``report_editor`` applies the operation itself through Power BI
Desktop afterwards.

What the handlers rely on, measured or reviewed:

- The local DAX engine is advisory (review P36): it refuses DAX it does not support by design,
  so no handler asks it to evaluate a formula. Structure is checked here (blank text, control
  characters, quotes and brackets that never close, a formula that refers to itself); whether
  the formula is right is settled by the editor's probe in Desktop, which is authoritative.
- A rename (P22) rewrites every report reference through ``report_refs`` (fields, formatting
  rules, sorts, filters that name the table through a ``From`` alias, bookmarks, report-level
  measures). ``queryRef`` and selector metadata stay as they are: Desktop keeps ``queryRef``
  stable across a measure rename. Analysis Services does not rewrite other measures' DAX, so
  each dependent measure becomes an ``UpdateMeasure`` in ``HandlerResult.model_operations``, which
  ``apply_operations`` places after the rename; calculated columns, calculated tables, field
  parameters and DAX query files cannot be rewritten from here and are named in the warnings.
- A delete is refused while anything uses the measure, listing the visuals that need it even
  through other measures.
- ``UpdateMeasure.hidden`` is refused: the Desktop applier drives ``pbi measure update``, which
  has no hide flag, so accepting it would report a change that is never made.
- The handlers update the ``ModelInfo`` they are given (the ``report_editor`` rebuilds the
  model before the change from the saved file and relies on it): a later operation of the same
  batch sees the created, renamed or deleted measure.

To add an operation: define its class in ``report_edit_ops``, add it to ``MeasureOperation`` and
its handler to ``MEASURE_HANDLERS``, and import both in the ``measures`` block of
``report_edits``; a model-layer class also joins ``MODEL_OPERATIONS``.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from .pbir_document import DocumentError, PbirDocument
from .report_edit_context import Handler, HandlerResult, ModelInfo
from .report_edit_ops import (
    CreateMeasure,
    DeleteMeasure,
    RenameMeasure,
    UpdateMeasure,
    operation_classes,
)
from .report_refs import (
    MODEL_PART,
    ReferenceIndex,
    dax_references,
    delete_guard,
    rename_dax_references,
    rename_measure_references,
)

MeasureOperation = CreateMeasure | UpdateMeasure | RenameMeasure | DeleteMeasure
MEASURE_OPERATIONS = operation_classes(MeasureOperation)

MAX_NAME = 128
MAX_FORMULA = 20000  # what ``UpdateMeasure.expression`` accepts
# Control characters other than tab, line feed and carriage return, which formulas and
# descriptions may hold.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ANY_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


# --- what the chat or the API sent is not trusted -------------------------------------------


def _name_problem(name: str, what: str) -> str | None:
    if not name.strip():
        return f"Give the {what} a name"
    if name != name.strip():
        return f"The {what} name cannot start or end with a space"
    if len(name) > MAX_NAME:
        return f"The {what} name is too long: Power BI names hold at most {MAX_NAME} characters"
    if _ANY_CONTROL.search(name):
        return f"The {what} name cannot contain line breaks or other control characters"
    return None


def _text_problem(text: str, what: str, *, lines: bool) -> str | None:
    if (_CONTROL if lines else _ANY_CONTROL).search(text):
        return f"The {what} cannot contain control characters"
    return None


def _format_problem(text: str) -> str | None:
    problem = _text_problem(text, "format string", lines=False)
    if problem is None and text.count('"') % 2:
        return "The format string has a quote that is never closed"
    return problem


def _dax_problem(text: str) -> str | None:
    """Why the formula cannot be well formed, from its quotes, brackets and comments alone."""
    if not text.strip():
        return "The measure needs a formula"
    problem = _text_problem(text, "formula", lines=True)
    if problem is not None:
        return problem
    depth, i, size = 0, 0, len(text)
    while i < size:
        char, pair = text[i], text[i : i + 2]
        if pair in ("//", "--"):
            end = text.find("\n", i)
            i = size if end < 0 else end
            continue
        if pair == "/*":
            end = text.find("*/", i + 2)
            if end < 0:
                return "The formula has a comment that is never closed"
            i = end + 2
            continue
        if char in "\"'[":
            closer = "]" if char == "[" else char
            j = i + 1
            while j < size:
                if text[j] == closer:
                    if text[j + 1 : j + 2] == closer:  # a doubled quote or ]] is an escape
                        j += 2
                        continue
                    break
                j += 1
            if j >= size:
                what = {'"': "text", "'": "table name", "[": "bracket"}[char]
                return f"The formula has a {what} that is never closed"
            i = j + 1
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return "The formula closes a parenthesis that was never opened"
        i += 1
    return "The formula has a parenthesis that is never closed" if depth else None


def _refuse(problem: str | None) -> None:
    if problem is not None:
        raise DocumentError(problem)


def _pasted_definition(name: str, expression: str) -> str | None:
    """A formula pasted as 'Name = ...' instead of just the DAX after the equals sign."""
    pattern = r"^\s*'?\[?" + re.escape(name) + r"\]?'?\s*=(?!=)"
    if re.match(pattern, expression, flags=re.IGNORECASE):
        return "Give just the formula, without the measure's name and equals sign before it"
    return None


# --- the model as the measures see it -------------------------------------------------------


def _quoted(table: str, name: str) -> str:
    return "'" + table.replace("'", "''") + "'[" + name.replace("]", "]]") + "]"


def _index(document: PbirDocument, model: ModelInfo) -> ReferenceIndex:
    """Every reference in the report and, for the DAX the model was given, in the model.

    A measure whose DAX is not known still goes in (with empty text), so that DAX which uses
    it, like a calculated column's, resolves to it."""
    measures = {
        _quoted(table, name): model.measure_expressions.get(name, "")
        for name, table in model.measures.items()
    }
    return ReferenceIndex.build(document, measures=measures, calculated=model.calculated)


def _column_clash(model: ModelInfo, table: str, name: str) -> str | None:
    home = (model.table(table) or "").casefold()
    for known, columns in model.columns.items():
        if known.casefold() == home and any(c.casefold() == name.casefold() for c in columns):
            return f"The table {known} already has a column called {name!r}"
    return None


def _unknown_references(model: ModelInfo, expression: str) -> list[str]:
    """``Table[Name]`` references to a known table that has no such column or measure. Studio's
    knowledge of the model can be older than Desktop's, so this only warns."""
    found: list[str] = []
    for ref in dax_references(expression):
        if ref.table is None or ref.name is None:
            continue
        table = model.table(ref.table)
        if table is None:
            continue
        known = {c.casefold() for c in model.columns.get(table, {})}
        if ref.name.casefold() in known or model.measure_table(ref.name):
            continue
        text = f"{ref.table}[{ref.name}]"
        if text not in found:
            found.append(text)
    return found


def _reference_warnings(model: ModelInfo, expression: str) -> list[str]:
    missing = _unknown_references(model, expression)
    if not missing:
        return []
    shown = ", ".join(missing[:3]) + (f" and {len(missing) - 3} more" if len(missing) > 3 else "")
    return [
        f"The formula uses {shown}, which Studio cannot find in the model. Power BI will check "
        "it, and the change is not kept if Power BI rejects it."
    ]


def _circular(document: PbirDocument, model: ModelInfo, name: str, expression: str) -> None:
    """Refuse a formula that refers to the measure itself or to one that already uses it."""
    home = (model.measure_table(name) or "").casefold()
    used = [(r.table, r.name) for r in dax_references(expression) if r.name is not None]
    for table, used_name in used:
        if used_name.casefold() == name.casefold() and (table is None or table.casefold() == home):
            raise DocumentError(f"[{name}] cannot use itself in its own formula")
    if not model.measure_expressions:
        return
    dependents = {
        i.name.casefold()
        for i in _index(document, model).dependent_items(name)
        if i.kind == "measure"
    }
    for used_table, used_name in used:
        if used_name.casefold() not in dependents:
            continue
        # Sales[Amount] is the column, not the measure [Amount] that depends on this one: only
        # a bare name, or the measure's own table, names the measure.
        dependent_home = (model.measure_table(used_name) or "").casefold()
        if used_table is None or used_table.casefold() == dependent_home:
            raise DocumentError(
                f"[{name}] cannot use [{used_name}]: [{used_name}] already depends on [{name}], "
                "so the two would refer to each other"
            )


# --- create ---------------------------------------------------------------------------------


def _check_create_measure(
    document: PbirDocument, op: CreateMeasure, model: ModelInfo
) -> HandlerResult:
    _refuse(_name_problem(op.name, "measure"))
    if model.measure_table(op.name):
        raise DocumentError(f"A measure named {op.name!r} already exists")
    table = model.table(op.table)
    if table is None:
        raise DocumentError(f"The model has no table {op.table!r}")
    _refuse(_column_clash(model, table, op.name))
    _refuse(_dax_problem(op.expression))
    _refuse(_pasted_definition(op.name, op.expression))
    if op.format_string is not None:
        _refuse(_format_problem(op.format_string))
    if op.folder is not None:
        _refuse(_text_problem(op.folder, "display folder", lines=False))
    if op.description is not None:
        _refuse(_text_problem(op.description, "description", lines=True))
    warnings = _reference_warnings(model, op.expression)
    model.measures[op.name] = table
    model.measure_expressions[op.name] = op.expression
    return HandlerResult(f"Created measure [{op.name}] in {op.table}.", warnings=warnings)


# --- update ---------------------------------------------------------------------------------

_UPDATE_FIELDS = ("expression", "format_string", "folder", "description", "hidden")


def _check_update_measure(
    document: PbirDocument, op: UpdateMeasure, model: ModelInfo
) -> HandlerResult:
    real = model.measure_name(op.name)
    if real is None:
        raise DocumentError(f"There is no measure named {op.name!r}")
    changed = [k for k in _UPDATE_FIELDS if getattr(op, k) is not None]
    if not changed:
        raise DocumentError("Say what to change in the measure")
    if op.hidden is not None:
        # ``pbi measure update`` has no flag for it (only ``measure create`` has --hidden), so
        # the change would be reported as done and never made.
        raise DocumentError(
            f"Studio cannot hide or show a measure yet. In Power BI Desktop, right-click "
            f"[{real}] in the Data pane and choose {'Hide' if op.hidden else 'Unhide'}"
        )
    warnings: list[str] = []
    if op.expression is not None:
        _refuse(_dax_problem(op.expression))
        _refuse(_pasted_definition(real, op.expression))
        _circular(document, model, real, op.expression)
        warnings = _reference_warnings(model, op.expression)
    if op.format_string is not None:
        _refuse(_format_problem(op.format_string))
    if op.folder is not None:
        _refuse(_text_problem(op.folder, "display folder", lines=False))
    if op.description is not None:
        _refuse(_text_problem(op.description, "description", lines=True))
    if op.expression is not None:
        model.measure_expressions[real] = op.expression
    return HandlerResult(
        f"Updated measure [{op.name}] ({', '.join(c.replace('_', ' ') for c in changed)}).",
        warnings=warnings,
    )


# --- rename ---------------------------------------------------------------------------------

# What a rename cannot rewrite from here, by the use kinds of report_refs.
_NOT_REWRITTEN = ("dax_query", "calculated_column", "calculated_table", "field_parameter")


def _check_rename_measure(
    document: PbirDocument, op: RenameMeasure, model: ModelInfo
) -> HandlerResult:
    table = model.measure_table(op.name)
    real = model.measure_name(op.name)
    if not table or real is None:
        raise DocumentError(f"There is no measure named {op.name!r}")
    if op.new_name == real:
        raise DocumentError(f"The measure is already called {op.new_name!r}")
    # Only the letter case may change onto the measure's own name.
    if op.new_name.casefold() != real.casefold() and model.measure_table(op.new_name):
        raise DocumentError(f"A measure named {op.new_name!r} already exists")
    _refuse(_name_problem(op.new_name, "measure"))
    _refuse(_column_clash(model, table, op.new_name))
    index = _index(document, model)
    # Every dependent's new formula is built before anything is written: one that cannot be
    # (too long for an UpdateMeasure) refuses the whole rename.
    updates: list[UpdateMeasure] = []
    for item in index.dependent_items(real):
        if item.kind != "measure" or item.part != MODEL_PART:
            continue  # columns and tables are warned about below; report-level ones were rewritten
        text, count = rename_dax_references(item.expression, table, real, op.new_name)
        if count:
            try:
                updates.append(UpdateMeasure(name=item.name, expression=text))
            except ValidationError as error:
                raise DocumentError(
                    f"The formula of [{item.name}] uses [{real}] and is too long for Studio to "
                    f"rewrite ({MAX_FORMULA} characters at most). Rename [{real}] in Power BI "
                    "instead, which lets you change that formula too."
                ) from error
    result = rename_measure_references(document, table, real, op.new_name, index=index)
    for update in updates:
        model.measure_expressions[update.name] = str(update.expression)
    warnings: list[str] = []
    for use in result.unrewritten:
        if use.kind in _NOT_REWRITTEN:
            warning = (
                f"{use.label} still uses [{real}]: also update it to [{op.new_name}] in Power BI"
            )
            if warning not in warnings:
                warnings.append(warning)
    _move_in_model(model, real, op.new_name)
    summary = _rename_summary(op)
    if updates:
        names = ", ".join(f"[{u.name}]" for u in updates[:3])
        more = f" and {len(updates) - 3} more" if len(updates) > 3 else ""
        summary = summary[:-1] + f", and the formulas of {names}{more}."
    return HandlerResult(summary, warnings=warnings, model_operations=list(updates))


def _rename_report_part(
    document: PbirDocument, op: RenameMeasure, model: ModelInfo
) -> HandlerResult:
    """A rename's half that lives in the report, run on the file Desktop saved after it renamed
    the measure (``model`` already has the new name): rewrite the references."""
    table = model.measure_table(op.new_name) or model.measure_table(op.name)
    if table is None:
        raise DocumentError(f"There is no measure named {op.new_name!r}")
    rename_measure_references(document, table, op.name, op.new_name)
    return HandlerResult(_rename_summary(op))


def _rename_summary(op: RenameMeasure) -> str:
    return f"Renamed measure [{op.name}] to [{op.new_name}] and updated the visuals that use it."


def _move_in_model(model: ModelInfo, old: str, new: str) -> None:
    table = model.measures.pop(old)
    model.measures[new] = table
    if old in model.measure_expressions:
        model.measure_expressions[new] = model.measure_expressions.pop(old)
    facts = model.facts.pop((table.casefold(), old.casefold()), None)
    if facts is not None:
        model.facts[(table.casefold(), new.casefold())] = facts


# --- delete ---------------------------------------------------------------------------------


def _check_delete_measure(
    document: PbirDocument, op: DeleteMeasure, model: ModelInfo
) -> HandlerResult:
    table = model.measure_table(op.name)
    real = model.measure_name(op.name)
    if not table or real is None:
        raise DocumentError(f"There is no measure named {op.name!r}")
    reasons = delete_guard(_index(document, model), real, table)
    if reasons:
        raise DocumentError(f"[{op.name}] is {'; '.join(reasons)}. Remove or change those first.")
    model.measures.pop(real)
    model.measure_expressions.pop(real, None)
    model.facts.pop((table.casefold(), real.casefold()), None)
    return HandlerResult(f"Deleted measure [{op.name}].")


# --- the pre-family helpers -----------------------------------------------------------------
# Kept because ``report_edits`` re-exported them before the split. They read ``Entity`` only, so
# nothing calls them any more: the handlers above go through ``report_refs``.


def _rename_references(
    node: Any, table: str, old: str, new: str, old_ref: str, new_ref: str
) -> bool:
    changed = False
    if isinstance(node, dict):
        measure = node.get("Measure")
        if (
            isinstance(measure, dict)
            and measure.get("Property") == old
            and measure.get("Expression", {}).get("SourceRef", {}).get("Entity") == table
        ):
            measure["Property"] = new
            changed = True
        for key in ("queryRef", "metadata"):
            if node.get(key) == old_ref:
                node[key] = new_ref
                changed = True
        if node.get("nativeQueryRef") == old:
            node["nativeQueryRef"] = new
            changed = True
        for child in node.values():
            changed = _rename_references(child, table, old, new, old_ref, new_ref) or changed
    elif isinstance(node, list):
        for child in node:
            changed = _rename_references(child, table, old, new, old_ref, new_ref) or changed
    return changed


def _uses_measure(node: Any, table: str, name: str) -> bool:
    if isinstance(node, dict):
        measure = node.get("Measure")
        if (
            isinstance(measure, dict)
            and measure.get("Property") == name
            and measure.get("Expression", {}).get("SourceRef", {}).get("Entity") == table
        ):
            return True
        return any(_uses_measure(child, table, name) for child in node.values())
    if isinstance(node, list):
        return any(_uses_measure(child, table, name) for child in node)
    return False


# What each model operation does to the report once Desktop has applied its model half
# (``apply_report_operations``). Only a rename touches the report.
MEASURE_REPORT_HANDLERS: dict[str, Handler] = {"rename_measure": _rename_report_part}

MEASURE_HANDLERS: dict[str, Handler] = {
    "create_measure": _check_create_measure,
    "update_measure": _check_update_measure,
    "rename_measure": _check_rename_measure,
    "delete_measure": _check_delete_measure,
}
