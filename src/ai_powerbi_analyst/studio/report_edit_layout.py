"""Layout operations: placing, stacking, hiding, deleting and duplicating visuals, adding a
visual, changing its type, and setting its fields and sort.

What each one relies on, so it stays consistent with the rest of the report:

- Positions are page pixels (``report_edit_geometry``): a member of a group stores its
  position relative to the group, so every write converts, and every bounds or overlap
  check uses page pixels. Moving a group moves only the group; resizing one is refused.
- ``AddVisual`` and ``ChangeVisualType`` place fields with ``report_roles`` (a hand-written
  role table, never the catalog's alphabetical order) and carry formatting across a type
  change instead of dropping it. A new visual goes to free space, gets the next tab stop and
  no invented title (Desktop titles it by itself).
- ``DeleteVisual`` and ``DuplicateVisual`` keep every reference to a visual name valid
  (``report_refs``): interactions, bookmarks, groups, filter names and tab order.
- ``SetFields`` and ``SetSort`` match fields by what they are (table, column, aggregation,
  hierarchy level), never by a caption, and leave fields Studio cannot edit alone.

The alignment operations and the two visuals that hold no fields (text box, shape) live in
``report_edit_align``; they are part of this family and listed in ``LayoutOperation``. To add
an operation: define its class in a family module, add it to ``LayoutOperation`` and its
handler to ``LAYOUT_HANDLERS``, and import the class in the ``layout`` block of
``report_edits``.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any

from . import pbir_expr as expr
from .pbir_document import DocumentError, PbirDocument
from .report_edit_align import ALIGN_HANDLERS, AlignOperation
from .report_edit_context import (
    Choice,
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    VisibleIn,
    _objects,
    _page_bounds,
    _query_state,
    _visual_type,
)
from .report_edit_geometry import (
    CROWDED,
    TOLERANCE,
    advisories,
    ancestors,
    attach,
    choose_placement,
    clamp,
    descendants,
    describe,
    is_group,
    join_names,
    leaf_boxes,
    move_box,
    new_visual,
    next_z,
    number,
    page_box,
    resolve_leaf,
    resolve_page,
    resolve_visual,
    sentence,
    show,
    siblings,
    tidy,
)
from .report_edit_ops import (
    AddVisual,
    Arrange,
    ChangeVisualType,
    DeleteVisual,
    DuplicateVisual,
    FieldRef,
    SetFields,
    SetPosition,
    SetSort,
    SetVisualHidden,
    operation_classes,
)
from .report_refs import remap_duplicate_visual, remove_visual_references
from .report_roles import (
    ADDABLE,
    AGGREGATION_CODES,
    FIELDLESS,
    Box,
    FieldSlot,
    NeedsAggregation,
    PlacementError,
    automatic_title,
    default_aggregation,
    default_size,
    default_title_visible,
    drop_field_references,
    field_identity,
    is_editable_projection,
    match_projection,
    place_fields,
    plan_type_change,
    projection_label,
    role_by_name,
    role_display_name,
    validate_query_state,
    visual_type_name,
)

LayoutOperation = (
    SetPosition
    | Arrange
    | SetVisualHidden
    | DeleteVisual
    | DuplicateVisual
    | AddVisual
    | ChangeVisualType
    | SetFields
    | SetSort
    | AlignOperation
)
LAYOUT_OPERATIONS = operation_classes(LayoutOperation)

# The visuals Studio's canvas draws (apps/desktop importedFormat ADDABLE, plus the new card).
# The others are written correctly and show in Power BI; the reply says so.
_DRAWN = frozenset(
    {
        "clusteredColumnChart",
        "columnChart",
        "clusteredBarChart",
        "barChart",
        "lineChart",
        "areaChart",
        "stackedAreaChart",
        "pieChart",
        "donutChart",
        "card",
        "cardVisual",
        "tableEx",
        "slicer",
    }
)


def _a(visual_type: str) -> str:
    name = visual_type_name(visual_type)
    return f"{'an' if name[:1].lower() in 'aeiou' else 'a'} {name}"


def _not_drawn(op_name: str, visual_type: str) -> list[VisibleIn]:
    if visual_type in _DRAWN:
        return []
    return [
        VisibleIn(
            op_name,
            "powerbi_only",
            f"Studio does not draw {_a(visual_type)} yet; it shows in Power BI.",
        )
    ]


def _apply_set_position(document: PbirDocument, op: SetPosition, model: ModelInfo) -> HandlerResult:
    """x and y are page pixels. A group member's stored value is relative to its group, so
    the group origin is subtracted on the way in; the visual is kept inside the page."""
    page, visual = resolve_visual(document, op.visual)
    name = str(visual["name"])
    if op.x is None and op.y is None and op.width is None and op.height is None:
        raise DocumentError("Say where to move it (x and y) or how big to make it (width, height)")
    label = describe(visual, page)
    box = page_box(page, name)
    group = is_group(visual)
    resizes = (op.width is not None and abs(op.width - box.width) > TOLERANCE) or (
        op.height is not None and abs(op.height - box.height) > TOLERANCE
    )
    if group and resizes:
        # Desktop's ScaleMode rescales a group's members when it is resized; Studio has not
        # measured that, so it does not guess.
        raise DocumentError(
            f"{sentence(label)} cannot be resized here. Resize the members individually, or "
            "ungroup in Power BI Desktop."
        )
    page_width, page_height = _page_bounds(page)
    width = min(op.width if op.width is not None else box.width, page_width)
    height = min(op.height if op.height is not None else box.height, page_height)
    x = clamp(op.x if op.x is not None else box.x, 0, page_width - width)
    y = clamp(op.y if op.y is not None else box.y, 0, page_height - height)
    before = leaf_boxes(page)
    move_box(
        document,
        page,
        name,
        x=x,
        y=y,
        width=None if group else width,
        height=None if group else height,
    )
    moved = op.x is not None or op.y is not None
    resized = op.width is not None or op.height is not None
    if moved and resized:
        summary = (
            f"Moved and resized {label} to {show(width)} x {show(height)} at "
            f"({show(x)}, {show(y)})."
        )
    elif moved:
        summary = f"Moved {label} to ({show(x)}, {show(y)})."
    else:
        summary = f"Resized {label} to {show(width)} x {show(height)}."
    clipped = (
        (op.width is not None and op.width > page_width)
        or (op.height is not None and op.height > page_height)
        or (op.x is not None and abs(op.x - x) > TOLERANCE)
        or (op.y is not None and abs(op.y - y) > TOLERANCE)
    )
    if clipped:
        summary += " It was kept inside the page."
    return HandlerResult(summary, warnings=advisories(page, [name], before=before))


_ORDER_WORDS = {
    "front": "at the front",
    "back": "at the back",
    "forward": "at the front",
    "backward": "at the back",
}


def _apply_arrange(document: PbirDocument, op: Arrange, model: ModelInfo) -> HandlerResult:
    """Stacking order among the visuals that share a parent (the page, or one group): a
    group's members are ordered among themselves and stay together with the group."""
    page, visual = resolve_visual(document, op.visual)
    name = str(visual["name"])
    label = describe(visual, page)
    family = siblings(page, name)

    def z_of(other: str) -> float:
        position = page.visuals[other].get("position")
        return number(position.get("z")) if isinstance(position, dict) else 0.0

    ordered = sorted(family, key=lambda other: (z_of(other), family.index(other)))
    index = ordered.index(name)
    last = len(ordered) - 1
    target = {
        "front": last,
        "back": 0,
        "forward": min(index + 1, last),
        "backward": max(index - 1, 0),
    }[op.to]
    if target == index:
        return HandlerResult(f"{sentence(label)} is already {_ORDER_WORDS[op.to]}.")
    values = sorted(z_of(other) for other in ordered)
    reordered = [other for other in ordered if other != name]
    reordered.insert(target, name)
    taken = {z_of(other) for other in page.visuals if other != name}
    new: dict[str, float] = {}
    if len(set(values)) != len(values):
        # Equal z values cannot be told apart by Desktop either: renumber the family.
        # Start where the family was and skip values the rest of the page uses, so z stays
        # unique page-wide.
        outside = {z_of(other) for other in page.visuals if other not in family}
        z = values[0]
        for other in reordered:
            while z in outside:
                z += 1000
            new[other] = z
            z += 1000
    elif op.to == "front":
        z = values[-1] + 1000
        while z in taken:
            z += 1
        new = {name: z}
    elif op.to == "back" and values[0] - 1000 >= 0 and values[0] - 1000 not in taken:
        new = {name: values[0] - 1000}
    else:
        # One step (or to the very back at z 0): the family trades z values, so every value
        # stays unique and only the visuals between change.
        new = dict(zip(reordered, values, strict=True))
    for other, z in new.items():
        position = page.visuals[other].setdefault("position", {})
        if abs(number(position.get("z"), -1.0) - z) > 0.01 or "z" not in position:
            position["z"] = tidy(z)
            document.touch_visual(page, other)
    words = {
        "front": f"Brought {label} to the front",
        "back": f"Sent {label} to the back",
        "forward": f"Moved {label} one step forward",
        "backward": f"Moved {label} one step backward",
    }[op.to]
    inside = " of its group" if ancestors(page, name) else ""
    return HandlerResult(f"{words}{inside}.")


def _apply_set_visual_hidden(
    document: PbirDocument, op: SetVisualHidden, model: ModelInfo
) -> HandlerResult:
    page, visual = resolve_visual(document, op.visual)
    label = describe(visual, page)
    if (visual.get("isHidden") is True) == op.hidden:
        return HandlerResult(f"{sentence(label)} was already {'hidden' if op.hidden else 'shown'}.")
    if op.hidden:
        visual["isHidden"] = True
    else:
        visual.pop("isHidden", None)
    document.touch_visual(page, str(visual["name"]))
    return HandlerResult(f"{'Hid' if op.hidden else 'Showed'} {label}.")


def _apply_delete_visual(
    document: PbirDocument, op: DeleteVisual, model: ModelInfo
) -> HandlerResult:
    """Delete a visual and every reference to it (interactions, bookmarks). A group takes
    its members with it, nested groups included, and a group left with no members goes too:
    Desktop does not keep empty groups."""
    page, visual = resolve_visual(document, op.visual)
    name = str(visual["name"])
    label = describe(visual, page)
    parent = visual.get("parentGroupName")
    result = remove_visual_references(document, page, name)
    inside = len(result.deleted) - 1
    counts = dict(result.counts)
    emptied: list[str] = []
    while (
        isinstance(parent, str)
        and parent in page.visuals
        and not any(v.get("parentGroupName") == parent for v in page.visuals.values())
    ):
        group = page.visuals[parent]
        emptied.append(describe(group, page))
        grand = group.get("parentGroupName")
        more = remove_visual_references(document, page, parent)
        for kind, amount in more.counts.items():
            counts[kind] = counts.get(kind, 0) + amount
        parent = grand
    summary = f"Deleted {label}"
    if inside:
        summary += f" and the {inside} visual{'' if inside == 1 else 's'} in it"
    if emptied:
        summary += f" and {join_names(emptied)}, which had nothing else in it"
    summary += "."
    cleaned = []
    if counts.get("visual_interaction"):
        n = counts["visual_interaction"]
        cleaned.append(f"{n} interaction setting{'' if n == 1 else 's'}")
    if counts.get("bookmark"):
        n = counts["bookmark"]
        cleaned.append(f"{n} bookmark{'' if n == 1 else 's'}")
    if cleaned:
        summary += f" It was also removed from {join_names(cleaned)}."
    return HandlerResult(summary)


def _apply_duplicate_visual(
    document: PbirDocument, op: DuplicateVisual, model: ModelInfo
) -> HandlerResult:
    """Copy a visual, or a group with everything in it, to free space on the same or another
    page. The copy gets new names and filter names (unique report-wide) and the next tab stop
    (``remap_duplicate_visual``), leaves its group unless the group is copied with it, and
    starts with the default interactions: nothing in ``visualInteractions`` points at it."""
    source_page, source = resolve_visual(document, op.visual)
    source_name = str(source["name"])
    target = resolve_page(document, op.page) if op.page else source_page
    label = describe(source, source_page)
    members = [source_name, *descendants(source_page, source_name)]
    used = document.visual_names()
    mapping: dict[str, str] = {}
    for old in members:
        mapping[old] = document.new_name(used | set(mapping.values()))
    box = page_box(source_page, source_name)
    grouped = is_group(source)
    page_width, page_height = _page_bounds(target)
    if (
        grouped
        and target is not source_page
        and (box.width > page_width + TOLERANCE or box.height > page_height + TOLERANCE)
    ):
        # A group's members keep their sizes, so it cannot be squeezed onto a smaller page.
        raise DocumentError(
            f"{sentence(label)} is {show(box.width)} by {show(box.height)} and page "
            f"'{target.display_name}' is only {show(page_width)} by {show(page_height)}. "
            "Resize the members individually or duplicate them one by one."
        )
    width = box.width if grouped else min(box.width, page_width)
    height = box.height if grouped else min(box.height, page_height)
    if target is source_page:
        prefer = Box(box.x, box.y, page_width - box.x, page_height - box.y)
    else:
        prefer = Box(box.x, box.y, width, height)
    placement = choose_placement(target, width, height, op.x, op.y, prefer=prefer)
    copies: dict[str, dict[str, Any]] = {}
    for old in members:
        copied = copy.deepcopy(source_page.visuals[old])
        copied["name"] = mapping[old]
        parent = copied.get("parentGroupName")
        if old != source_name and isinstance(parent, str) and parent in mapping:
            copied["parentGroupName"] = mapping[parent]
        copies[old] = copied
    top = copies[source_name].setdefault("position", {})
    top["x"], top["y"] = tidy(placement.x), tidy(placement.y)
    if not grouped:
        top["width"], top["height"] = tidy(placement.width), tidy(placement.height)
    base = next_z(target)

    def z_of(old: str) -> float:
        position = source_page.visuals[old].get("position")
        return number(position.get("z")) if isinstance(position, dict) else 0.0

    for rank, old in enumerate(sorted(members, key=z_of)):
        copies[old].setdefault("position", {})["z"] = base + 1000 * rank
    before = leaf_boxes(target)
    for old in members:
        attach(document, target, copies[old])
    for old in members:
        remap_duplicate_visual(document, target, mapping[old], copied_groups=set(mapping.values()))
    warnings = advisories(
        target, [mapping[source_name]], before=before, together=set(mapping.values())
    )
    if placement.crowded:
        warnings.insert(0, CROWDED)
    summary = f"Duplicated {label}"
    if len(members) > 1:
        summary += f" and the {len(members) - 1} visual{'' if len(members) == 2 else 's'} in it"
    return HandlerResult(
        f"{summary} onto page '{target.display_name}'.",
        warnings=warnings,
        lineage={mapping[old]: old for old in members},
    )


# Studio adds these types; others (maps, Python, Q&A, custom visuals) need Desktop.
ADDABLE_TYPES = ADDABLE


def _assign_roles(
    visual_type: str, projections: list[tuple[str, dict[str, Any]]]
) -> dict[str, list[dict[str, Any]]]:
    """Place (kind, projection) pairs into the type's roles (``report_roles.place_fields``).
    Kept under its old name for callers of the pre-split module."""
    slots = [FieldSlot("measure" if kind == "measure" else "column", p) for kind, p in projections]
    try:
        return place_fields(visual_type, slots)
    except PlacementError as error:
        raise DocumentError(str(error)) from error


# --- building projections -------------------------------------------------------------------

_FUNCTION_LABELS = {
    "Sum": "Sum",
    "Average": "Average",
    "Count": "Count (distinct)",
    "CountNonNull": "Count",
    "Min": "Minimum",
    "Max": "Maximum",
}
_CARD_TYPES = frozenset({"card", "cardVisual"})


def _plain(kind: str, table: str, name: str) -> dict[str, Any]:
    field = expr.measure_ref(table, name) if kind == "measure" else expr.column_ref(table, name)
    return {"field": field, "queryRef": f"{table}.{name}", "nativeQueryRef": name}


def _aggregated(table: str, name: str, function: str) -> dict[str, Any]:
    """A column summarized the way Desktop writes it: ``Sum(Table.Col)`` with the caption
    "Sum of Col"."""
    projection: dict[str, Any] = {
        "field": {
            "Aggregation": {
                "Expression": expr.column_ref(table, name),
                "Function": AGGREGATION_CODES[function],
            }
        },
        "queryRef": f"{function}({table}.{name})",
    }
    projection["nativeQueryRef"] = projection_label(projection)
    return projection


def _default_function(model: ModelInfo, table: str, name: str) -> str:
    """How a column is summarized on a value role: its SummarizeBy, never a silent Sum.
    Raises NeedsAggregation for a column with no default (dates, keys) or a text column."""
    if not model.is_numeric(table, name):
        raise NeedsAggregation(
            f"{name} is not a number, so it cannot be added up.", ("Count", "CountNonNull")
        )
    return default_aggregation(model.summarize_by(table, name))


def _aggregation_choice(
    refs: Sequence[FieldRef], index: int, name: str, error: NeedsAggregation, key: str = "fields"
) -> NeedsChoice:
    """Ask how to summarize a column; each answer is the field list with that function set.
    ``name`` is the column's own spelling in the model."""
    options = []
    for function in error.options:
        changed = [r.model_dump(mode="json", exclude_none=True) for r in refs]
        changed[index]["aggregation"] = function
        options.append(Choice(f"{_FUNCTION_LABELS[function]} of {name}", {key: changed}))
    return NeedsChoice(
        f"How should {name} be summarized? Power BI has no default summary for it.", options
    )


def _slots(
    model: ModelInfo, refs: Sequence[FieldRef], functions: dict[int, str]
) -> list[FieldSlot]:
    slots: list[FieldSlot] = []
    for index, ref in enumerate(refs):
        kind, table, name = model.resolve(ref)
        function = ref.aggregation or functions.get(index)
        if kind == "measure":
            slots.append(FieldSlot("measure", _plain("measure", table, name)))
        elif function:
            slots.append(FieldSlot("measure", _aggregated(table, name, function)))
        else:
            slots.append(FieldSlot("column", _plain("column", table, name)))
    return slots


def _place(
    model: ModelInfo, visual_type: str, refs: Sequence[FieldRef], small: Sequence[FieldRef]
) -> dict[str, list[dict[str, Any]]]:
    """Put the fields in the type's roles. A number with no aggregation stays a plain column
    (a table lists it, a chart uses it as a category) unless the visual cannot be built that
    way, and then it becomes the value, summarized by its default (or asked about)."""
    small_slots = [
        FieldSlot("measure" if kind == "measure" else "column", _plain(kind, table, name))
        for kind, table, name in (model.resolve(ref) for ref in small)
    ]
    try:
        return place_fields(visual_type, _slots(model, refs, {}), small_multiples=small_slots)
    except PlacementError as first:
        bare = [
            i
            for i, ref in enumerate(refs)
            if ref.aggregation is None
            and (found := model.resolve(ref))[0] == "column"
            and model.is_numeric(found[1], found[2])
        ]
        for count in range(1, len(bare) + 1):
            chosen = bare[-count:]
            try:
                # Does the visual work with these as values? (Sum stands in for the real
                # function, which decides nothing about where a field goes.)
                place_fields(
                    visual_type,
                    _slots(model, refs, dict.fromkeys(chosen, "Sum")),
                    small_multiples=small_slots,
                )
            except PlacementError:
                continue
            functions: dict[int, str] = {}
            for index in chosen:
                _, table, name = model.resolve(refs[index])
                try:
                    functions[index] = _default_function(model, table, name)
                except NeedsAggregation as needs:
                    raise _aggregation_choice(refs, index, name, needs) from needs
            return place_fields(
                visual_type, _slots(model, refs, functions), small_multiples=small_slots
            )
        raise DocumentError(str(first)) from None


def _apply_add_visual(document: PbirDocument, op: AddVisual, model: ModelInfo) -> HandlerResult:
    visual_type = op.visual_type
    if visual_type in FIELDLESS:
        raise DocumentError(
            "A text box holds text, a shape its look and a picture a file, not fields, so "
            "they are added on their own rather than from fields"
        )
    if visual_type not in expr.visual_types() or visual_type not in ADDABLE_TYPES:
        raise DocumentError(f"Studio cannot add a {visual_type} visual yet")
    page = resolve_page(document, op.page)
    # A field listed twice would be written twice with the same query name; once is enough.
    fields: list[FieldRef] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for ref in op.fields:
        kind, table, name = model.resolve(ref)
        key = (kind, table.casefold(), name.casefold(), ref.aggregation)
        if key not in seen:
            seen.add(key)
            fields.append(ref)
    try:
        placed = _place(model, visual_type, fields, op.small_multiples)
    except PlacementError as error:
        raise DocumentError(str(error)) from error
    default_width, default_height = default_size(visual_type)
    placement = choose_placement(
        page,
        op.width or default_width,
        op.height or default_height,
        op.x,
        op.y,
        # A size nobody asked for may shrink to fit a real gap rather than cover a visual.
        min_scale=1.0 if op.width or op.height else 0.5,
    )
    visual = new_visual(document, page, visual_type, placement)
    state = {role: {"projections": items} for role, items in placed.items()}
    visual["visual"] = {
        "visualType": visual_type,
        "query": {"queryState": state},
        "drillFilterOtherVisuals": True,
    }
    if op.title:
        expr.set_property(
            _objects(visual, "visualContainerObjects"), "title", "text", expr.string(op.title)
        )
        expr.set_property(
            _objects(visual, "visualContainerObjects"), "title", "show", expr.boolean(True)
        )
    before = leaf_boxes(page)
    attach(document, page, visual)
    warnings = advisories(page, [str(visual["name"])], before=before)
    if placement.crowded:
        warnings.insert(0, CROWDED)
    labels = [projection_label(p) for items in placed.values() for p in items]
    summary = f"Added {_a(visual_type)} of {join_names(labels, 8)} to page '{page.display_name}'."
    automatic = automatic_title(visual_type, state)
    if op.title:
        summary += f" Its title is '{op.title}'."
    elif automatic and default_title_visible(visual_type):
        summary += f" Power BI titles it '{automatic}' by itself."
    return HandlerResult(
        summary, warnings=warnings, visible_in=_not_drawn("add_visual", visual_type)
    )


def _count_settings(objects: dict[str, Any]) -> int:
    return sum(
        len(entry["properties"]) if isinstance(entry.get("properties"), dict) else 1
        for entries in objects.values()
        for entry in entries
        if isinstance(entry, dict)
    )


def _apply_change_visual_type(
    document: PbirDocument, op: ChangeVisualType, model: ModelInfo
) -> HandlerResult:
    """Turn a visual into another type keeping what still applies: fields go where they mean
    the same thing (a column chart's axis is a pie's legend), every formatting setting the new
    type has is carried over (``plan_type_change``), and the box, filters, group and position
    are untouched. What could not come along is listed."""
    page, visual = resolve_leaf(document, op.visual)
    old = _visual_type(visual)
    if op.visual_type not in expr.visual_types() or op.visual_type not in ADDABLE_TYPES:
        raise DocumentError(f"Studio cannot turn a visual into a {op.visual_type} yet")
    label = describe(visual, page)
    holds_fields = any(bucket.get("projections") for bucket in _query_state(visual).values())
    if old != op.visual_type and (old in FIELDLESS or not holds_fields):
        raise DocumentError(
            f"{sentence(label)} has no fields to move, so it cannot become "
            f"{_a(op.visual_type)}. Add a {visual_type_name(op.visual_type)} instead."
        )
    try:
        plan = plan_type_change(old, op.visual_type, visual)
    except PlacementError as error:
        raise DocumentError(str(error)) from error
    body = visual["visual"]
    body["visualType"] = op.visual_type
    body.setdefault("query", {})["queryState"] = plan.query_state
    if plan.objects:
        body["objects"] = plan.objects
    else:
        body.pop("objects", None)
    notes = drop_field_references(visual, plan.removed)
    document.touch_visual(page, str(visual["name"]))
    target_name = _a(op.visual_type)
    if visual_type_name(old) == visual_type_name(op.visual_type) and old != op.visual_type:
        # The two card types read alike: say which one the visual became.
        target_name = "a new card" if op.visual_type == "cardVisual" else "a classic card"
    summary = f"Changed {label} into {target_name}."
    if old == op.visual_type:
        summary = f"{sentence(label)} already is {_a(op.visual_type)}."
    else:
        kept = _count_settings(plan.objects)
        if kept:
            summary += f" Kept {kept} formatting setting{'' if kept == 1 else 's'}."
        summary += "".join(f" {line}" for line in [*plan.dropped, *notes])
    return HandlerResult(
        summary,
        warnings=list(plan.warnings),
        visible_in=_not_drawn("change_visual_type", op.visual_type),
    )


# --- fields ---------------------------------------------------------------------------------


def _matches(projection: dict[str, Any], ref: FieldRef) -> bool:
    """Whether the field reference names this projection: by what the field is (table and
    column or measure, aggregation); a hierarchy level, which has no column of its own, by the
    level's caption within its table; a visual calculation, which has no table, by its name."""
    if match_projection(projection, ref):
        return True
    identity = field_identity(projection)
    if identity[0] == "HierarchyLevel":
        return identity[1].casefold() == ref.table.casefold() and match_projection(
            projection, ref.name
        )
    return identity[0] == "Unknown" and match_projection(projection, ref.name)


def _uneditable_reason(projection: dict[str, Any], parameter_tables: set[str]) -> str | None:
    """Why Studio will not edit this field, or None when it can."""
    text = json.dumps(projection.get("field", {}))
    if "PropertyVariationSource" in text:
        return "an automatic date hierarchy"
    if "NativeVisualCalculation" in text:
        return "a visual calculation"
    identity = field_identity(projection)
    if identity[0] in {"Column", "Measure"} and identity[1].casefold() in parameter_tables:
        return "a field parameter"
    if not is_editable_projection(projection):
        return "a field Studio does not know how to edit"
    return None


def _refuse_uneditable(projections: Sequence[dict[str, Any]], tables: set[str]) -> None:
    for projection in projections:
        reason = _uneditable_reason(projection, tables)
        if reason:
            raise DocumentError(
                f"{projection_label(projection)} is {reason}, which Studio leaves as it is. "
                "Change it in Power BI Desktop."
            )


def _field_bucket(
    model: ModelInfo,
    refs: Sequence[FieldRef],
    index: int,
    *,
    value_role: bool,
    visual_type: str,
    parameter_tables: set[str],
) -> dict[str, Any]:
    """A new projection for ``refs[index]``. On a value role a number is summarized by its
    default (asked about when it has none), and text is counted, never added as a bare column,
    except on a card, which shows text values as they are."""
    ref = refs[index]
    kind, table, name = model.resolve(ref)
    if table.casefold() in parameter_tables:
        raise DocumentError(
            f"{name} belongs to a field parameter, which Studio cannot put in a visual. Add it "
            "in Power BI Desktop."
        )
    if kind == "measure":
        return _plain("measure", table, name)
    function: str | None = ref.aggregation
    if (
        function is None
        and value_role
        and (model.is_numeric(table, name) or visual_type not in _CARD_TYPES)
    ):
        try:
            function = _default_function(model, table, name)
        except NeedsAggregation as needs:
            raise _aggregation_choice(refs, index, name, needs) from needs
    return _aggregated(table, name, function) if function else _plain("column", table, name)


def _apply_set_fields(document: PbirDocument, op: SetFields, model: ModelInfo) -> HandlerResult:
    page, visual = resolve_leaf(document, op.visual)
    visual_type = _visual_type(visual)
    label = describe(visual, page)
    role = role_by_name(visual_type, op.role)
    catalog = expr.roles(visual_type)["roles"] if visual_type in expr.visual_types() else {}
    if role is None or role not in catalog:
        wells = join_names([role_display_name(visual_type, r) for r in catalog], 12)
        raise DocumentError(
            f"{sentence(_a(visual_type))} has no {op.role!r} field well."
            + (f" Its field wells are {wells}." if wells else "")
        )
    display = role_display_name(visual_type, role)
    value_role = catalog[role].get("kind") == "Measure"
    parameter_tables = {t.casefold() for t in model.field_parameter_tables()}
    state = _query_state(visual)
    bucket: dict[str, Any] = dict(state.get(role) or {})
    current: list[dict[str, Any]] = list(bucket.get("projections", []))
    removed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    already: list[str] = []
    if op.mode == "remove":
        gone = [p for p in current if any(_matches(p, ref) for ref in op.fields)]
        if not gone:
            held = join_names([projection_label(p) for p in current]) or "nothing"
            raise DocumentError(f"None of those fields are in {display}. It holds {held}.")
        _refuse_uneditable(gone, parameter_tables)
        removed = gone
        result = [p for p in current if not any(p is g for g in gone)]
    else:
        result = list(current) if op.mode == "add" else []
        for index, ref in enumerate(op.fields):
            # A field already in the result (the role's own, or listed twice in this
            # operation) is never written a second time: Desktop would show two identical
            # columns or series.
            listed = next((p for p in result if _matches(p, ref)), None)
            if listed is not None:
                if op.mode == "add" and not any(listed is a for a in added):
                    already.append(projection_label(listed))
                continue
            twin = (
                next((p for p in current if _matches(p, ref)), None)
                if op.mode == "replace"
                else None
            )
            if twin is not None:
                result.append(twin)
                continue
            fresh = _field_bucket(
                model,
                op.fields,
                index,
                value_role=value_role,
                visual_type=visual_type,
                parameter_tables=parameter_tables,
            )
            added.append(fresh)
            result.append(fresh)
        if op.mode == "replace":
            removed = [p for p in current if not any(p is r for r in result)]
            _refuse_uneditable(removed, parameter_tables)
    trial = {**bucket, "projections": result}
    # Only this role is checked here (a card with no field yet is a state the next step of a
    # batch can fix); the whole document is validated when the batch ends.
    problems = [
        p
        for p in validate_query_state(visual_type, {role: trial})
        if " needs a field for " not in p
    ]
    if problems:
        raise DocumentError(problems[0])
    if result or set(bucket) - {"projections"}:
        state[role] = trial
    else:
        state.pop(role, None)
    notes = drop_field_references(visual, removed)
    document.touch_visual(page, str(visual["name"]))
    if op.mode == "remove":
        summary = (
            f"Removed {join_names([projection_label(p) for p in removed], 8)} from {display} of "
            f"{label}."
        )
    elif op.mode == "add":
        names = [projection_label(p) for p in added]
        summary = (
            f"Added {join_names(names, 8)} to {display} of {label}."
            if names
            else f"{sentence(join_names(already, 8))} already in {display} of {label}."
        )
    else:
        names = [projection_label(p) for p in result]
        summary = f"Set {display} of {label} to {join_names(names, 8) if names else 'nothing'}."
    if op.mode == "add" and added and already:
        summary += (
            f" {sentence(join_names(already, 8))} {'was' if len(already) == 1 else 'were'} "
            "already there."
        )
    if notes:
        summary += " " + " ".join(notes)
    return HandlerResult(summary)


def _drop_selectors(visual: dict[str, Any], query_refs: set[str]) -> None:
    """Remove formatting entries and sorts that point at fields no longer in the visual.
    The operations no longer call it (``drop_field_references`` matches by field identity,
    so a renamed measure's sort goes too); the name stays for callers of the pre-split module."""
    if not query_refs:
        return
    for where in ("objects", "visualContainerObjects"):
        objects = visual.get("visual", {}).get(where, {})
        for name in list(objects):
            objects[name] = [
                e for e in objects[name] if e.get("selector", {}).get("metadata") not in query_refs
            ]
            if not objects[name]:
                objects.pop(name)
    sort = visual.get("visual", {}).get("query", {}).get("sortDefinition")
    if sort:
        kept = []
        for item in sort.get("sort", []):
            field_ = item.get("field", {})
            prop = (field_.get("Measure") or field_.get("Column") or {}).get("Property")
            entity = (
                (field_.get("Measure") or field_.get("Column") or {})
                .get("Expression", {})
                .get("SourceRef", {})
                .get("Entity")
            )
            if f"{entity}.{prop}" not in query_refs:
                kept.append(item)
        if kept:
            sort["sort"] = kept
        else:
            visual["visual"]["query"].pop("sortDefinition", None)


def _apply_set_sort(document: PbirDocument, op: SetSort, model: ModelInfo) -> HandlerResult:
    """Sort by a field that is in the visual. The sort item carries the projection's own field
    expression, so it names the field the way Desktop does (an aggregated column stays an
    aggregation, a hierarchy level a hierarchy level)."""
    page, visual = resolve_leaf(document, op.visual)
    projection = next(
        (
            p
            for bucket in _query_state(visual).values()
            for p in bucket.get("projections", [])
            if _matches(p, op.field)
        ),
        None,
    )
    if projection is None:
        _, _, missing = model.resolve(op.field)  # an unknown field is the model's error first
        raise DocumentError(f"Add {missing} to the visual before sorting by it")
    if "HierarchyLevel" in projection["field"]:
        name = projection_label(projection)  # a level is not a model column or measure
    else:
        name = model.resolve(op.field)[2]
    reason = _uneditable_reason(projection, {t.casefold() for t in model.field_parameter_tables()})
    if reason:
        raise DocumentError(
            f"{projection_label(projection)} is {reason}, which Studio cannot sort by. "
            "Sort it in Power BI Desktop."
        )
    # Desktop writes ``isDefaultSort: true`` only for its automatic sort and leaves the key out
    # of a sort the user chose (the hand-sorted visuals in the fixtures), so it is omitted.
    visual["visual"].setdefault("query", {})["sortDefinition"] = {
        "sort": [
            {
                "field": copy.deepcopy(projection["field"]),
                "direction": op.direction.capitalize(),
            }
        ],
    }
    document.touch_visual(page, str(visual["name"]))
    return HandlerResult(f"Sorted {describe(visual, page)} by {name}, {op.direction}.")


LAYOUT_HANDLERS: dict[str, Handler] = {
    "set_position": _apply_set_position,
    "arrange": _apply_arrange,
    "set_visual_hidden": _apply_set_visual_hidden,
    "delete_visual": _apply_delete_visual,
    "duplicate_visual": _apply_duplicate_visual,
    "add_visual": _apply_add_visual,
    "change_visual_type": _apply_change_visual_type,
    "set_fields": _apply_set_fields,
    "set_sort": _apply_set_sort,
    **ALIGN_HANDLERS,
}
