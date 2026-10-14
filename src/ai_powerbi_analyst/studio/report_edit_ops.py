"""The values and the operation classes of the report edit layer: data only, no handlers.

Every operation is a closed pydantic model with an ``op`` literal, so a batch arrives as
plain dicts (from chat or the HTTP API) and validates into exactly one class. This module
holds the classes the first version shipped with, and the values they share (colors, field
references). Family modules import from here and from ``report_edit_context``; nothing here
imports a family module, so there is no import cycle.

Where a new operation goes: define the class in your family module (report_edit_layout,
report_edit_pages, report_edit_measures...) next to its handler, add it to that module's
``*Operation`` union and its handler to ``*_HANDLERS``. ``report_edits`` builds the
``ReportOperation`` union from those unions, so nobody edits this file to add an operation.
Only a change to a value several families share (a new color form, say) belongs here.

Studio does not change styling (colors, fonts, titles, data labels, legends, axes, a visual's
box, page backgrounds, themes): the operations that did were removed, and chat refuses such a
request (``edit_capabilities``). The colors below are still values of the operations that
remain (a shape's fill, a conditional format's colors, an analytics line).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from ai_powerbi_analyst.contracts import ClosedModel

# --- values -------------------------------------------------------------------------

HexColor = Annotated[str, Field(pattern=r"^#?[0-9A-Fa-f]{6}$")]


class ThemeColorRef(ClosedModel):
    """A theme color: 0 white, 1 black, 2..9 the theme's first eight data colors (measured in
    Desktop). Larger ids pass this check so that the handler can refuse them with the
    report's actual swatches; Desktop draws them in the default fill."""

    theme: int = Field(ge=0, le=41)
    percent: float = Field(default=0, ge=-1, le=1)


ColorValue = HexColor | ThemeColorRef


def _color_arg(value: ColorValue) -> str | dict[str, Any]:
    return (
        {"theme": value.theme, "percent": value.percent}
        if isinstance(value, ThemeColorRef)
        else value
    )


class FieldRef(ClosedModel):
    table: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    # "Sum", "Average", "Count"… for a numeric column placed where a measure goes.
    aggregation: Literal["Sum", "Average", "Min", "Max", "Count", "CountNonNull"] | None = None


def operation_classes(union: Any) -> tuple[type[ClosedModel], ...]:
    """The classes of a union of operations, or of one class: what ``LAYOUT_OPERATIONS`` and
    its siblings hold, derived from the union so a family lists each operation once."""
    members = get_args(union)
    found: tuple[type[ClosedModel], ...] = members if members else (union,)
    return found


# --- operations: layout, visuals and their fields ------------------------------------


class SetPosition(ClosedModel):
    op: Literal["set_position"] = "set_position"
    visual: str
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=10, le=20000)
    height: float | None = Field(default=None, ge=10, le=20000)


class Arrange(ClosedModel):
    op: Literal["arrange"] = "arrange"
    visual: str
    to: Literal["front", "back", "forward", "backward"]


class SetVisualHidden(ClosedModel):
    op: Literal["set_visual_hidden"] = "set_visual_hidden"
    visual: str
    hidden: bool


class DeleteVisual(ClosedModel):
    op: Literal["delete_visual"] = "delete_visual"
    visual: str


class DuplicateVisual(ClosedModel):
    op: Literal["duplicate_visual"] = "duplicate_visual"
    visual: str
    page: str | None = None
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)


class AddVisual(ClosedModel):
    """A new visual on a page. Without x and y it goes to free space (a new chart no longer
    lands on top of the slicers); without a size it gets the type's usual one; without a
    title Power BI titles it from its fields. ``small_multiples`` (a column) splits the visual
    into one copy per value; fields go there only when named here."""

    op: Literal["add_visual"] = "add_visual"
    page: str
    visual_type: str = Field(min_length=1, max_length=60)
    fields: list[FieldRef] = Field(min_length=1, max_length=12)
    small_multiples: list[FieldRef] = Field(default_factory=list, max_length=1)
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=40, le=20000)
    height: float | None = Field(default=None, ge=40, le=20000)
    title: str | None = Field(default=None, max_length=300)


class ChangeVisualType(ClosedModel):
    op: Literal["change_visual_type"] = "change_visual_type"
    visual: str
    visual_type: str = Field(min_length=1, max_length=60)


class SetFields(ClosedModel):
    op: Literal["set_fields"] = "set_fields"
    visual: str
    role: str = Field(min_length=1, max_length=40)
    fields: list[FieldRef] = Field(default_factory=list, max_length=12)
    mode: Literal["replace", "add", "remove"] = "replace"


class SetSort(ClosedModel):
    op: Literal["set_sort"] = "set_sort"
    visual: str
    field: FieldRef
    direction: Literal["ascending", "descending"] = "descending"


# --- operations: pages ------------------------------------------------------------------


class RenamePage(ClosedModel):
    op: Literal["rename_page"] = "rename_page"
    page: str
    name: str = Field(min_length=1, max_length=100)


class AddPage(ClosedModel):
    op: Literal["add_page"] = "add_page"
    name: str = Field(min_length=1, max_length=100)
    width: float = Field(default=1280, ge=100, le=20000)
    height: float = Field(default=720, ge=100, le=20000)
    after: str | None = None


class DuplicatePage(ClosedModel):
    op: Literal["duplicate_page"] = "duplicate_page"
    page: str
    name: str | None = Field(default=None, max_length=100)


class DeletePage(ClosedModel):
    op: Literal["delete_page"] = "delete_page"
    page: str
    # The answer to NeedsConfirmation("remove_page_references"): switch off the buttons,
    # tooltips, drillthrough links and bookmark states that point at the page, then delete it.
    cleanup_references: bool = False


class MovePage(ClosedModel):
    op: Literal["move_page"] = "move_page"
    page: str
    index: int = Field(ge=0, le=500)


class SetPageSize(ClosedModel):
    op: Literal["set_page_size"] = "set_page_size"
    page: str
    width: float = Field(ge=100, le=20000)
    height: float = Field(ge=100, le=20000)
    display: Literal["FitToPage", "FitToWidth", "ActualSize"] | None = None
    # True scales every visual with the page; False keeps them where they are and refuses
    # when any would fall outside the new size.
    scale: bool = True


class SetPageHidden(ClosedModel):
    op: Literal["set_page_hidden"] = "set_page_hidden"
    page: str
    hidden: bool


# --- operations: model layer ------------------------------------------------------------
# Validated in report_edit_measures, applied through Power BI Desktop.


class CreateMeasure(ClosedModel):
    op: Literal["create_measure"] = "create_measure"
    table: str = Field(min_length=1, max_length=200)
    # 128 is the longest name Studio will create; ``name`` of the other operations only has to
    # find a measure that already exists, so it keeps the wider bound.
    name: str = Field(min_length=1, max_length=128)
    expression: str = Field(min_length=1, max_length=20000)
    format_string: str | None = Field(default=None, max_length=200)
    folder: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)


class UpdateMeasure(ClosedModel):
    op: Literal["update_measure"] = "update_measure"
    name: str = Field(min_length=1, max_length=200)
    expression: str | None = Field(default=None, min_length=1, max_length=20000)
    format_string: str | None = Field(default=None, max_length=200)
    folder: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    # Hide the measure from the Fields pane (True) or show it again (False).
    hidden: bool | None = None

    @model_serializer(mode="wrap")
    def _leave_out_unset_hidden(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        # Batches recorded before ``hidden`` existed have no such key; an unset one is not
        # written, so the journal and the receipts of an unchanged operation stay as they were.
        data: dict[str, Any] = handler(self)
        if data.get("hidden") is None:
            data.pop("hidden", None)
        return data


class RenameMeasure(ClosedModel):
    op: Literal["rename_measure"] = "rename_measure"
    name: str = Field(min_length=1, max_length=200)
    new_name: str = Field(min_length=1, max_length=128)


class DeleteMeasure(ClosedModel):
    op: Literal["delete_measure"] = "delete_measure"
    name: str = Field(min_length=1, max_length=200)
