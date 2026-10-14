"""Navigation and interactivity of an imported report: bookmarks, buttons and navigators,
drillthrough and tooltip pages, edit interactions, synced slicers and drill state.

Where the shapes come from. A shape Power BI Desktop 2.157 saved itself in the Agents dashboard
is trusted (``DESKTOP_AUTHORED_SHAPES``: a visual's ``drillFilterOtherVisuals``, the report's
``settings.defaultDrillFilterOtherVisuals``, a projection's ``active`` flag). Everything else was
read from what Desktop 2.157 ships in its install folder (``bin/WebView2Resources/minerva/
scripts``): the PBIR schemas it validates with (``desktop.schema.json.*``: page 2.1.0, bookmark
2.1.0, bookmarksMetadata 1.0.0, visualContainer 2.12.0) and its own writers in ``desktop.min.js``
(``PageSerializer``, ``BookmarkSerializer``, ``ExplorationStateSerializer``,
``addVisualContainerActionButtonVisual``, ``addDrillthroughFilter``, the sync-group code). That is
strong evidence but not a file Desktop saved, so each such shape is listed in
``UNVERIFIED_SHAPES`` with what it rests on, and ``DESKTOP_PROBES`` are the batches a person opens
in Desktop to settle them. Findings from Desktop's code that correct the hand-built multipage
fixture: a report-page tooltip is ``visualTooltip.type = 'Canvas'`` (not ``'ReportPage'``), button
icons are lower camel case (``back``, ``leftArrow``; ``Forward`` does not exist), a bookmark's
visual formatting sits under ``objects.merge``, and a bookmark only limits itself to its
``targetVisualNames`` when ``applyOnlyToTargetVisuals`` is true.

What every operation keeps consistent (``navigation_problems`` checks it and the tests run it
after each operation):

- A bookmark is ``bookmarks/<id>.bookmark.json`` plus exactly one entry in
  ``bookmarks/bookmarks.json`` (top level, or a child of one group). Buttons point at a bookmark's
  id (``visualLink.bookmark``), a bookmark navigator at a group's id (``bookmarks.bookmarkGroup``)
  and at a bookmark (``deselectionBookmark``). Deleting a bookmark something points at asks first
  (``NeedsConfirmation("remove_bookmark_references")``; ``report_refs.bookmark_references`` finds
  them). A group that is ungrouped, or left empty by a delete or a move (Desktop drops empty
  groups), takes its navigators to every bookmark, with a warning.
- Pages are referenced by folder name: buttons (``navigationSection``, ``drillthroughSection``),
  tooltips (``visualTooltip.section``), bookmarks (``activeSection``). A drillthrough or tooltip
  page's fields are ``pageBinding.parameters`` bound (``boundFilter``) to page filters created with
  ``howCreated: Drillthrough``; turning the page back into a normal page removes both, and asks
  first when buttons or tooltips still use it (``NeedsConfirmation("clear_page_links")``).
- Visuals are referenced by name from ``visualInteractions`` (same page) and bookmark states;
  synced slicers share ``visual.syncGroup.groupName`` and are copies of one another.

Receipts follow what ``render_navigation`` and ``live_report`` do: Studio's view mode lists and
applies bookmarks, draws a button or navigator once ``report_tokens.family_of`` gives its type a
family and follows the actions it knows (back, bookmark, page, drillthrough; a web address and
Q&A are Power BI's), resolves every interaction type, and draws the copy of a synced slicer.
Drill state, drillthrough and page tooltips, and syncing itself say ``powerbi_only``.

The family is registered by the integration step; until then ``apply_navigation`` applies a batch
the way ``report_edits.apply_operations`` does.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field, TypeAdapter, ValidationError

from ai_powerbi_analyst.contracts import ClosedModel

from . import pbir_expr as expr
from .edit_tools import ProbeCase, Slot, ToolCard
from .pbir_document import (
    BOOKMARKS_META,
    BOOKMARKS_METADATA_SCHEMA,
    DocumentError,
    PageDoc,
    PbirDocument,
    Unlisted,
)
from .render_navigation import INTERACTION_TYPES as RENDERED_INTERACTIONS
from .report_edit_context import (
    EditOutcome,
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsChoice,
    NeedsConfirmation,
    VisibleIn,
)
from .report_edit_geometry import (
    attach,
    choose_placement,
    describe,
    is_group,
    join_names,
    new_visual,
    next_tab_order,
    next_z,
    page_box,
    resolve_page,
    resolve_visual,
    sentence,
    tidy,
    visual_kind,
)
from .report_edit_ops import operation_classes
from .report_filters import collect_filter_names
from .report_refs import (
    BookmarkReference,
    NavigationFinding,
    bookmark_references,
    clear_bookmark_references,
    remap_duplicate_visual,
    remove_visual_references,
)
from .report_refs import navigation_findings as refs_navigation_findings
from .report_roles import projection_label
from .report_tokens import family_of, property_is_rendered

__all__ = [
    "BOOKMARK_SCHEMA",
    "BOOKMARKS_METADATA_SCHEMA",
    "DESKTOP_AUTHORED_SHAPES",
    "DESKTOP_PROBES",
    "EXAMPLE_SETUP",
    "NAVIGATION_HANDLERS",
    "NAVIGATION_OPERATIONS",
    "TOOL_CARDS",
    "TOOLTIP_SIZES",
    "UNVERIFIED_SHAPES",
    "AddBookmark",
    "AddButton",
    "AddNavigator",
    "BookmarkReference",
    "ButtonAction",
    "ClearPageType",
    "DeleteBookmark",
    "DrillVisual",
    "GroupBookmarks",
    "MoveBookmark",
    "NavField",
    "NavigationFinding",
    "NavigationOperation",
    "RenameBookmark",
    "ResetInteractions",
    "SetBookmarkVisualHidden",
    "SetButtonAction",
    "SetDrillFilter",
    "SetDrillthrough",
    "SetInteraction",
    "SetNavigator",
    "SetTooltipPage",
    "SetVisualTooltip",
    "SyncSlicer",
    "UngroupBookmarks",
    "UnsyncSlicer",
    "UpdateBookmark",
    "apply_navigation",
    "bookmark_references",
    "navigation_findings",
    "navigation_problems",
    "parse_navigation",
]

FAMILY = "navigation"

# --- what the shapes rest on ---------------------------------------------------------------

_DEFINITION = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
# The versions Desktop 2.157 ships and validates with (desktop.schema.json.14 and .0).
BOOKMARK_SCHEMA = _DEFINITION + "bookmark/2.1.0/schema.json"
# (bookmarksMetadata/1.0.0 is ``pbir_document.BOOKMARKS_METADATA_SCHEMA``: the document makes
# bookmarks.json when the first bookmark of a report is added.)
# The exploration state's version: Desktop 2.157 re-saved every bookmark Studio wrote (display,
# data with filters, group trees) with "1.0" and kept the rest of the state (probes of
# 2026-09-29), so "1.0" is what a bookmark gets.
EXPLORATION_VERSION = "1.0"
BOOKMARK_PREFIX = "Bookmark"

DESKTOP_AUTHORED_SHAPES: dict[str, str] = {
    "visual.drillFilterOtherVisuals": "every visual of the Agents dashboard (true); Desktop drops "
    "a false one on save, so off is the key left out",
    "report.settings.defaultDrillFilterOtherVisuals": "the Agents dashboard's report.json (true); "
    "Desktop drops a false one on save, so off is the key left out",
    "projection.active": "Category projections of the Agents column charts (single level, true)",
    "bookmark.version": "explorationState.version '1.0': what Desktop 2.157 saved for every "
    "bookmark Studio wrote (probes of 2026-09-29)",
    "bookmarks.json.empty": "no bookmarks.json once the last bookmark is gone: Desktop 2.157 "
    "removed an empty list on save (probes of 2026-09-29)",
}

UNVERIFIED_SHAPES: dict[str, str] = {
    "bookmark.file": "bookmark/2.1.0 schema + BookmarkSerializer: $schema, displayName, name, "
    "options (only true flags), explorationState",
    "bookmark.name": "'Bookmark' + 20 hex characters, as the hand-built fixture and pbi-cli name "
    "them; Desktop's own generator was not found in its code",
    "bookmark.display_hidden": "ExplorationStateSerializer: singleVisual.display.mode = 'hidden' "
    "for a hidden visual, every visual of the page listed so the unlisted ones are not reset",
    "bookmark.groups": "visualContainerGroups tree {isHidden, children} with isHidden true or "
    "false on every group (ExplorationStateSerializer captures both; applying a bookmark only "
    "changes a group whose state has the flag)",
    "bookmark.options": "applyOnlyToTargetVisuals + targetVisualNames, suppressData, "
    "suppressDisplay, suppressActiveSection (BookmarkSerializer, applyBookmarkState)",
    "bookmark.data": "filter containers {name, type, filter, expression, howCreated as a number} "
    "sorted as Desktop's capture sorts them: hidden or locked in view mode under byName (by the "
    "filter's name), on a field under byExpr, include/exclude under byType; slicer selections "
    "under singleVisual.objects.merge, orderBy {Direction, Expression} "
    "and activeProjections {role: [expr]}: the legacy exploration format Desktop's bookmark "
    "serializer writes; a bookmark with Data on resets whatever it does not capture",
    "bookmarks.json": "bookmarksMetadata/1.0.0: items [{name}] and groups {name, displayName, "
    "children}",
    "button.insert": "addVisualContainerActionButtonVisual: icon.shapeType under {id: default}, "
    "visualContainerObjects.visualLink {show, type} for back, bookmark and Q&A, 100 x 40",
    "button.action": "visualLink type Back / Bookmark / PageNavigation / Drillthrough / WebUrl / "
    "Qna with bookmark, navigationSection, drillthroughSection, webUrl, tooltip",
    "button.slicers": "setSlicerActionButtonDefaults: icon shapeType applyAllSlicers / "
    "clearAllSlicers plus an unscoped icon show false, text show true and the words centred "
    "under {id: default}, visualLink {show, type ApplyAllSlicers / ClearAllSlicers, "
    "tooltipPlaceholderText Desktop's default tooltip}, 200 x 50",
    "navigator.insert": "addVisualContainerNavigatorVisual: a bare pageNavigator or "
    "bookmarkNavigator, 50 high, 200 per page (200..1000) or 600 wide",
    "navigator.options": "layout.orientation 2 horizontal, 1 vertical, 0 grid (+ rowCount, "
    "columnCount), pages.showHiddenPages / showTooltipPages, pages.showPage under {id: page}, "
    "bookmarks.bookmarkGroup = the group's name",
    "navigator.deselection": "bookmarkNavigator bookmarks.allowDeselectionBookmark true + "
    "deselectionBookmark = the bookmark's id (the property pair in Desktop's navigator property "
    "map); switched off, and on deleting that bookmark, allowDeselectionBookmark false without "
    "deselectionBookmark",
    "drillthrough.page": "page.type Drillthrough, pageBinding {name, type Drillthrough, "
    "parameters [{name, boundFilter, fieldExpr}], acceptsFilterContext None when 'keep all "
    "filters' is off, referenceScope CrossReport}, a page filter {name, field, type Categorical, "
    "howCreated Drillthrough}, and a Back button at 0, 0 (PageSerializer, addDrillthroughFilter)",
    "tooltip.page": "page.type Tooltip, Desktop's tooltip sizes 320 x 240 / 400 x 300 / 600 x 450, "
    "tooltip fields as a pageBinding of type Tooltip",
    "visual.tooltip": "visualContainerObjects.visualTooltip {show, type 'Canvas', section}",
    "interactions": "page.visualInteractions {source, target, type DataFilter / HighlightFilter / "
    "NoFilter}; a reset removes the entry (Desktop drops Default ones on load)",
    "sync.group": "visual.syncGroup {groupName, fieldChanges, filterChanges}; the group is named "
    "after the slicer's field with 1, 2... for a repeat; a copy on each page at the same place; "
    "a copy that is synced but not visible is hidden (isHidden)",
    "drill.levels": "every projection of a drilled role carries active true or false "
    "(the visual serializer writes the flag for each once a role has active ones)",
}

# PBIR's howCreated words as the numbers the exploration state (bookmarks) stores (enum 275275).
_HOW_CREATED_CODES = {
    "Auto": 0,
    "User": 1,
    "Drill": 2,
    "Include": 3,
    "Exclude": 4,
    "Drillthrough": 5,
}
# Desktop's page-size presets of the Tooltip canvas type ("Single visual", "Multiple visuals",
# "Mini report"). Desktop picks 400 x 300 when the canvas type is switched to Tooltip; Studio
# starts from the classic 320 x 240 unless asked, which Desktop lists as the same type.
TOOLTIP_SIZES: dict[str, tuple[int, int]] = {
    "single_visual": (320, 240),
    "multiple_visuals": (400, 300),
    "mini_report": (600, 450),
}
# The ribbon's Buttons menu: the icon each kind gets, and the action Desktop turns on with it.
_BUTTON_ICONS = {
    "blank": "blank",
    "back": "back",
    "bookmark": "bookmarks",
    "information": "information",
    "help": "help",
    "qna": "qna",
    "left_arrow": "leftArrow",
    "right_arrow": "rightArrow",
    "reset": "reset",
    "apply_all_slicers": "applyAllSlicers",
    "clear_all_slicers": "clearAllSlicers",
}
_DEFAULT_ACTIONS = {"back": "Back", "bookmark": "Bookmark", "qna": "Qna"}
_BUTTON_SIZE = (100.0, 40.0)  # VisualAuthoringService's default box for an inserted button
# The ribbon's "Apply all slicers" and "Clear all slicers" buttons (setSlicerActionButtonDefaults):
# the action type, the words (centred, icon off) and the tooltip Desktop shows until one is set,
# in a 200 x 50 box.
_SLICER_BUTTONS: dict[str, tuple[str, str, str]] = {
    "apply_all_slicers": (
        "ApplyAllSlicers",
        "Apply all slicers",
        "Apply all slicers on this page. Hiding all buttons will cause slicers to instantly "
        "apply on this page.",
    ),
    "clear_all_slicers": ("ClearAllSlicers", "Clear all slicers", "Clear all slicers on this page"),
}
_SLICER_BUTTON_SIZE = (200.0, 50.0)
_NAVIGATOR_HEIGHT = 50.0
_BUTTON_TYPES = frozenset({"actionButton", "pageNavigator", "bookmarkNavigator"})
_ORIENTATIONS = {"horizontal": 2, "vertical": 1, "grid": 0}
_INTERACTION_TYPES = {"filter": "DataFilter", "highlight": "HighlightFilter", "none": "NoFilter"}
_SYNC_TYPES = frozenset({"slicer", "advancedSlicerVisual"})
# Visuals that take an action (Format > Action), and those whose action list has no drillthrough.
_LINKABLE = frozenset({"actionButton", "shape", "basicShape", "image"})
_NO_DRILLTHROUGH_LINK = frozenset({"basicShape", "image"})
# Visuals that neither filter others nor are filtered: decoration and navigation.
_NOT_INTERACTIVE = frozenset(
    {
        "textbox",
        "shape",
        "basicShape",
        "image",
        "actionButton",
        "pageNavigator",
        "bookmarkNavigator",
    }
)
# Visuals that cannot show a cross-highlight: selecting a bar filters them instead.
_NO_HIGHLIGHT = frozenset(
    {
        "tableEx",
        "pivotTable",
        "card",
        "cardVisual",
        "multiRowCard",
        "slicer",
        "advancedSlicerVisual",
        "kpi",
        "gauge",
    }
)
_NOT_DRILLABLE = frozenset(
    {"tableEx", "card", "cardVisual", "multiRowCard", "slicer", "advancedSlicerVisual"}
)
_MAX_LISTED = 6


# --- values ---------------------------------------------------------------------------------


class NavField(ClosedModel):
    """A column for a drillthrough or tooltip page; the table may be left out when the name is
    unique in the model."""

    table: str | None = Field(default=None, min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


ActionType = Literal["back", "bookmark", "page", "drillthrough", "web_url", "qna", "none"]


class ButtonAction(ClosedModel):
    """What clicking a button (or a shape or picture) does. ``page`` names the page to go to
    (``page``) or to drill through to (``drillthrough``); ``url`` is an http(s) address."""

    type: ActionType
    bookmark: str | None = Field(default=None, min_length=1, max_length=200)
    page: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000)


VisualName = Annotated[str, Field(min_length=1, max_length=200)]  # an id, a caption, words
PageName = Annotated[str, Field(min_length=1, max_length=200)]  # an id or a display name
BookmarkName = Annotated[str, Field(min_length=1, max_length=200)]  # an id or a display name
ButtonKind = Literal[
    "blank",
    "back",
    "bookmark",
    "information",
    "help",
    "qna",
    "left_arrow",
    "right_arrow",
    "reset",
    "apply_all_slicers",
    "clear_all_slicers",
]


# --- operations: bookmarks ------------------------------------------------------------------


class AddBookmark(ClosedModel):
    """A bookmark of a page as it is now. What it remembers follows Desktop's options: ``display``
    (which visuals are hidden), ``current_page`` (opening it goes to its page) and ``data``
    (filters, slicer selections and sorts; off unless asked, because a bookmark that remembers
    data resets whatever filter it did not capture). ``visuals`` limits it to those visuals
    ("Selected visuals"); ``hide`` and ``show`` change the look of visuals in this bookmark only.
    """

    op: Literal["add_bookmark"] = "add_bookmark"
    name: str = Field(min_length=1, max_length=100)
    page: str | None = Field(default=None, min_length=1, max_length=200)
    data: bool = False
    display: bool = True
    current_page: bool = True
    visuals: list[VisualName] = Field(default_factory=list, max_length=100)
    hide: list[VisualName] = Field(default_factory=list, max_length=100)
    show: list[VisualName] = Field(default_factory=list, max_length=100)
    group: str | None = Field(default=None, min_length=1, max_length=100)


class UpdateBookmark(ClosedModel):
    """Desktop's "Update": take the page's current state again, and change what the bookmark
    remembers. ``visuals`` [] makes it apply to every visual again.

    Which visuals and groups the bookmark hid or showed is kept for those still on the page
    (the bookmark is Studio's only record of it; a new visual takes the page's state). Turning
    ``data`` on without ``recapture`` takes only the data half of the page. A change that makes
    the bookmark stop hiding something says so."""

    op: Literal["update_bookmark"] = "update_bookmark"
    bookmark: str = Field(min_length=1, max_length=200)
    recapture: bool = True
    data: bool | None = None
    display: bool | None = None
    current_page: bool | None = None
    visuals: list[VisualName] | None = Field(default=None, max_length=100)


class RenameBookmark(ClosedModel):
    op: Literal["rename_bookmark"] = "rename_bookmark"
    bookmark: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)


class MoveBookmark(ClosedModel):
    """Reorder the Bookmarks pane: ``position`` is 1-based within the bookmark's list (its group,
    or the top level); ``group`` moves it into a group, ``top_level`` out of its group."""

    op: Literal["move_bookmark"] = "move_bookmark"
    bookmark: str = Field(min_length=1, max_length=200)
    position: int | None = Field(default=None, ge=1, le=1000)
    group: str | None = Field(default=None, min_length=1, max_length=100)
    top_level: bool = False


class GroupBookmarks(ClosedModel):
    op: Literal["group_bookmarks"] = "group_bookmarks"
    bookmarks: list[BookmarkName] = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


class UngroupBookmarks(ClosedModel):
    op: Literal["ungroup_bookmarks"] = "ungroup_bookmarks"
    group: str = Field(min_length=1, max_length=100)


class DeleteBookmark(ClosedModel):
    """Delete a bookmark. When a button or a bookmark navigator uses it, the answer to the
    question (``cleanup_references``) switches those off first."""

    op: Literal["delete_bookmark"] = "delete_bookmark"
    bookmark: str = Field(min_length=1, max_length=200)
    cleanup_references: bool = False


class SetBookmarkVisualHidden(ClosedModel):
    """Hide (or show) a visual in one bookmark only; the page itself is not changed."""

    op: Literal["set_bookmark_visual_hidden"] = "set_bookmark_visual_hidden"
    bookmark: str = Field(min_length=1, max_length=200)
    visual: VisualName
    hidden: bool = True


# --- operations: buttons and navigators ------------------------------------------------------


class AddButton(ClosedModel):
    """A button from the Insert > Buttons menu, with Desktop's icon and default action for its
    kind (Back goes back, Q&A opens Q&A, Bookmark opens a bookmark); ``action`` sets another.
    "Apply all slicers" and "Clear all slicers" buttons do only that, and say so in words."""

    op: Literal["add_button"] = "add_button"
    page: str = Field(min_length=1, max_length=200)
    kind: ButtonKind = "blank"
    text: str | None = Field(default=None, min_length=1, max_length=200)
    action: ButtonAction | None = None
    tooltip: str | None = Field(default=None, min_length=1, max_length=500)
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=10, le=20000)
    height: float | None = Field(default=None, ge=10, le=20000)


class SetButtonAction(ClosedModel):
    """What a button, shape or picture does when clicked (``action.type`` "none" turns the
    action off), and the tooltip it shows."""

    op: Literal["set_button_action"] = "set_button_action"
    visual: VisualName
    action: ButtonAction | None = None
    tooltip: str | None = Field(default=None, max_length=500)


NavigatorOrientation = Literal["horizontal", "vertical", "grid"]


class AddNavigator(ClosedModel):
    """A page navigator (a button per page) or a bookmark navigator (a button per bookmark of a
    group, or of every bookmark)."""

    op: Literal["add_navigator"] = "add_navigator"
    page: str = Field(min_length=1, max_length=200)
    kind: Literal["page", "bookmark"] = "page"
    orientation: NavigatorOrientation | None = None
    rows: int | None = Field(default=None, ge=1, le=10)
    columns: int | None = Field(default=None, ge=1, le=10)
    bookmark_group: str | None = Field(default=None, min_length=1, max_length=100)
    show_hidden_pages: bool | None = None
    show_tooltip_pages: bool | None = None
    x: float | None = Field(default=None, ge=0, le=20000)
    y: float | None = Field(default=None, ge=0, le=20000)
    width: float | None = Field(default=None, ge=10, le=20000)
    height: float | None = Field(default=None, ge=10, le=20000)


class SetNavigator(ClosedModel):
    """A navigator's options: layout, which bookmark group, and which pages it lists
    (``bookmark_group`` "" lists every bookmark again). ``deselection_bookmark`` is the
    bookmark a bookmark navigator opens when its selected button is clicked again (Desktop's
    "Allow deselection" with "Launch bookmark on deselection"); "" switches that off."""

    op: Literal["set_navigator"] = "set_navigator"
    visual: VisualName
    orientation: NavigatorOrientation | None = None
    rows: int | None = Field(default=None, ge=1, le=10)
    columns: int | None = Field(default=None, ge=1, le=10)
    bookmark_group: str | None = Field(default=None, max_length=100)
    deselection_bookmark: str | None = Field(default=None, max_length=200)
    show_hidden_pages: bool | None = None
    show_tooltip_pages: bool | None = None
    show_pages: list[PageName] = Field(default_factory=list, max_length=100)
    hide_pages: list[PageName] = Field(default_factory=list, max_length=100)


# --- operations: drillthrough and tooltip pages -------------------------------------------------


class SetDrillthrough(ClosedModel):
    """Make a page a drillthrough page for columns (or add, replace or remove its drillthrough
    columns). ``keep_all_filters`` is Desktop's "Keep all filters" (on for a new page);
    ``back_button`` adds the Back button Desktop adds."""

    op: Literal["set_drillthrough"] = "set_drillthrough"
    page: str = Field(min_length=1, max_length=200)
    fields: list[NavField] = Field(min_length=1, max_length=10)
    mode: Literal["replace", "add", "remove"] = "replace"
    keep_all_filters: bool | None = None
    cross_report: bool | None = None
    back_button: bool = True


class SetTooltipPage(ClosedModel):
    """Make a page a report-page tooltip: tooltip-sized (``size`` "keep" leaves the size), hidden
    from readers unless ``hide`` is false, and optionally tied to fields (the tooltip then shows
    only for them)."""

    op: Literal["set_tooltip_page"] = "set_tooltip_page"
    page: str = Field(min_length=1, max_length=200)
    size: Literal["single_visual", "multiple_visuals", "mini_report", "keep"] = "single_visual"
    hide: bool = True
    fields: list[NavField] = Field(default_factory=list, max_length=10)
    mode: Literal["replace", "add", "remove"] = "replace"


class ClearPageType(ClosedModel):
    """Turn a drillthrough or tooltip page back into a normal page. When buttons drill through
    to it or visuals use it as their tooltip, the answer to the question
    (``cleanup_references``) switches those off first."""

    op: Literal["clear_page_type"] = "clear_page_type"
    page: str = Field(min_length=1, max_length=200)
    cleanup_references: bool = False


class SetVisualTooltip(ClosedModel):
    """Show a tooltip page when hovering a visual; without ``page`` the visual goes back to
    Power BI's default tooltip."""

    op: Literal["set_visual_tooltip"] = "set_visual_tooltip"
    visual: VisualName
    page: str | None = Field(default=None, min_length=1, max_length=200)


# --- operations: interactions, sync, drill ----------------------------------------------------


class SetInteraction(ClosedModel):
    """Edit interactions: how ``targets`` react when a data point of ``source`` is selected
    (every other visual on the page when ``targets`` is empty). "default" restores Power BI's
    own choice for the target type."""

    op: Literal["set_interaction"] = "set_interaction"
    source: VisualName
    targets: list[VisualName] = Field(default_factory=list, max_length=100)
    type: Literal["filter", "highlight", "none", "default"]


class ResetInteractions(ClosedModel):
    """Back to default interactions for one source visual, or for a whole page."""

    op: Literal["reset_interactions"] = "reset_interactions"
    visual: VisualName | None = None
    page: str | None = Field(default=None, min_length=1, max_length=200)


class SyncSlicer(ClosedModel):
    """Sync a slicer to other pages (Desktop's Sync slicers pane): each page gets a synced copy
    in the same place, or its matching slicer joins the group. On ``hidden_on`` pages the copy
    syncs but is not visible."""

    op: Literal["sync_slicer"] = "sync_slicer"
    visual: VisualName
    pages: list[PageName] = Field(default_factory=list, max_length=100)
    hidden_on: list[PageName] = Field(default_factory=list, max_length=100)
    group: str | None = Field(default=None, min_length=1, max_length=100)
    field_changes: bool = True
    filter_changes: bool = True


class UnsyncSlicer(ClosedModel):
    """Stop syncing: the group's slicers on ``pages`` (every page when empty) leave the group;
    ``delete_copies`` also deletes the ones that leave, except the slicer named."""

    op: Literal["unsync_slicer"] = "unsync_slicer"
    visual: VisualName
    pages: list[PageName] = Field(default_factory=list, max_length=100)
    delete_copies: bool = False


class SetDrillFilter(ClosedModel):
    """Whether drilling in one visual filters the others: for one visual, or the report's
    default (no ``visual``)."""

    op: Literal["set_drill_filter"] = "set_drill_filter"
    visual: VisualName | None = None
    filter_other_visuals: bool


class DrillVisual(ClosedModel):
    """Drill a chart or matrix on a hierarchy: go to the next level, expand all down one level,
    go up one level, or back to the top level."""

    op: Literal["drill_visual"] = "drill_visual"
    visual: VisualName
    action: Literal["next_level", "expand_all", "up", "top"]
    role: str | None = Field(default=None, min_length=1, max_length=40)


NavigationOperation = (
    AddBookmark
    | UpdateBookmark
    | RenameBookmark
    | MoveBookmark
    | GroupBookmarks
    | UngroupBookmarks
    | DeleteBookmark
    | SetBookmarkVisualHidden
    | AddButton
    | SetButtonAction
    | AddNavigator
    | SetNavigator
    | SetDrillthrough
    | SetTooltipPage
    | ClearPageType
    | SetVisualTooltip
    | SetInteraction
    | ResetInteractions
    | SyncSlicer
    | UnsyncSlicer
    | SetDrillFilter
    | DrillVisual
)
NAVIGATION_OPERATIONS = operation_classes(NavigationOperation)


# --- small shared helpers -----------------------------------------------------------------------


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _listed(items: Sequence[str]) -> str:
    shown = list(dict.fromkeys(items))[:_MAX_LISTED]
    more = len(dict.fromkeys(items)) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more else "")


def _all(items: Sequence[str]) -> str:
    """Every item joined ("a, b and c"): what one step changed is never cut to "and 1 more"."""
    return join_names(items, max(len(items), 1))


def _powerbi_only(op: str, detail: str) -> list[VisibleIn]:
    return [VisibleIn(op, "powerbi_only", detail)]


def _visual_name(visual: dict[str, Any]) -> str:
    return str(visual["name"])


def _button_text(visual: Mapping[str, Any]) -> str | None:
    """The words on a button in its default look, switched on or not (``report_captions``
    names a button by them only while they show)."""
    body = visual.get("visual")
    if not isinstance(body, dict) or body.get("visualType") != "actionButton":
        return None
    for entry in _entries(body.get("objects"), "text"):
        selector = entry.get("selector")
        props = entry.get("properties")
        if (not selector or selector == {"id": "default"}) and isinstance(props, dict):
            text = expr.decode(props.get("text"))
            if isinstance(text, str) and text.strip():
                return " ".join(text.split())
    return None


def _label(visual: dict[str, Any], page: PageDoc) -> str:
    """The visual as a person points at it: a button by its words, else ``describe``."""
    text = _button_text(visual)
    return f"the '{text}' button" if text else describe(visual, page=page)


_KIND_WORDS = {
    "page navigator": "pageNavigator",
    "bookmark navigator": "bookmarkNavigator",
    "button": "actionButton",
}


def _find_visual(document: PbirDocument, ref: str) -> tuple[PageDoc, dict[str, Any]]:
    """``resolve_visual`` (id or caption), then a button by its words, then the only page
    navigator, bookmark navigator or button of the report when ``ref`` names the kind."""
    try:
        return resolve_visual(document, ref)
    except DocumentError as error:
        wanted = " ".join(ref.split()).casefold()
        found = [
            (page, visual)
            for page, _, visual in document.all_visuals()
            if (_button_text(visual) or "").casefold() == wanted
        ]
        kind = _KIND_WORDS.get(wanted.removeprefix("the "))
        if not found and kind is not None:
            found = [(p, v) for p, _, v in document.all_visuals() if visual_kind(v) == kind]
        if len(found) == 1:
            return found[0]
        if found:
            places = [f"{_label(v, p)} on page {_page_label(p)}" for p, v in found]
            raise DocumentError(
                f"More than one visual is called {ref!r}: {join_names(places)}. Say which."
            ) from error
        raise


def _leaf(document: PbirDocument, ref: str) -> tuple[PageDoc, dict[str, Any]]:
    page, visual = _find_visual(document, ref)
    if is_group(visual):
        raise DocumentError(
            f"{sentence(_label(visual, page))} holds other visuals; name one of the visuals "
            "inside it"
        )
    return page, visual


def _container_objects(visual: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = visual.setdefault("visual", {})
    objects: dict[str, Any] = body.setdefault("visualContainerObjects", {})
    return objects


def _own_objects(visual: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = visual.setdefault("visual", {})
    objects: dict[str, Any] = body.setdefault("objects", {})
    return objects


def _entries(holder: Any, name: str) -> list[dict[str, Any]]:
    found = holder.get(name) if isinstance(holder, dict) else None
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def _props(entry: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = entry.setdefault("properties", {})
    return props


def _general_entry(holder: dict[str, Any], name: str) -> dict[str, Any]:
    """The selector-less entry of an object (made when missing): where Desktop keeps a visual's
    action and tooltip page."""
    entries = holder.setdefault(name, [])
    for entry in entries:
        if isinstance(entry, dict) and not entry.get("selector"):
            return entry
    added: dict[str, Any] = {"properties": {}}
    entries.append(added)
    return added


def _literal(node: Any) -> Any:
    return expr.decode(node)


def _drop_empty(holder: dict[str, Any], name: str) -> None:
    entries = holder.get(name)
    if isinstance(entries, list):
        kept = [e for e in entries if not (isinstance(e, dict) and not e.get("properties"))]
        if kept:
            holder[name] = kept
        else:
            holder.pop(name, None)


def _encode(visual_type: str, object_name: str, prop: str, value: Any) -> dict[str, Any]:
    """A property value as the catalog types it for this visual type, or a plain message."""
    spec = expr.property_spec(visual_type, object_name, prop)
    if spec is None:
        raise DocumentError(f"A {visual_type} has no {object_name} {prop} setting in Power BI")
    return expr.encode_property(spec[1], prop, value)


def _page_type(page: PageDoc) -> str | None:
    """ "Drillthrough", "Tooltip" or None for a normal page (the page's own type, else its
    binding's)."""
    kind = page.data.get("type")
    if kind in {"Drillthrough", "Tooltip"}:
        return str(kind)
    binding = page.data.get("pageBinding")
    if isinstance(binding, dict) and binding.get("type") in {"Drillthrough", "Tooltip"}:
        return str(binding["type"])
    return None


def _page_label(page: PageDoc) -> str:
    return f"'{page.display_name}'"


# --- bookmarks: storage ---------------------------------------------------------------------


def _touch_meta(document: PbirDocument) -> None:
    document.mark_dirty(BOOKMARKS_META)


def _released(document: PbirDocument, unlisted: Unlisted) -> tuple[list[str], list[str]]:
    """What taking bookmarks out of their groups did besides that: the groups it left empty
    (Desktop drops them), and the bookmark navigators that listed one of those groups and now
    list every bookmark. Returns (notes for the summary, warnings)."""
    notes = [f"the bookmark group '{g}' was left empty, so it was removed" for g in unlisted.groups]
    which = "that group" if len(unlisted.groups) <= 1 else "one of those groups"
    warnings = []
    for page_name, name in unlisted.navigators:
        page = document.pages[page_name]
        warnings.append(
            f"{sentence(_label(page.visuals[name], page))} listed {which}; it now lists every "
            "bookmark."
        )
    return notes, warnings


def _merge_unlisted(first: Unlisted, second: Unlisted) -> Unlisted:
    return Unlisted(first.groups + second.groups, first.navigators + second.navigators)


def _group_items(document: PbirDocument) -> list[dict[str, Any]]:
    items = document.bookmarks_metadata.get("items")
    return [
        i
        for i in (items if isinstance(items, list) else [])
        if isinstance(i, dict) and isinstance(i.get("children"), list)
    ]


def _bookmark_label(bookmark: Mapping[str, Any], key: str) -> str:
    return f"'{bookmark.get('displayName') or key}'"


def _bookmark_list(document: PbirDocument) -> str:
    names = [str(b.get("displayName") or k) for k, b in document.bookmarks().items()]
    return join_names([f"'{n}'" for n in names], 8) if names else "none yet"


def _resolve_bookmark(document: PbirDocument, ref: str) -> str:
    """A bookmark's id from its id or its display name (case-insensitive), with a message that
    lists the bookmarks when neither matches."""
    bookmarks = document.bookmarks()
    text = ref.strip()
    if text in bookmarks:
        return text
    matches = [
        k
        for k, b in bookmarks.items()
        if str(b.get("displayName", "")).casefold() == text.casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise DocumentError(f"{len(matches)} bookmarks are named {text!r}; rename one first")
    if any(_group_matches(g, text) for g in _group_items(document)):
        raise DocumentError(
            f"{text!r} is a group of bookmarks, not a bookmark; name a bookmark inside it, or "
            "ungroup it"
        )
    raise DocumentError(f"There is no bookmark {text!r}. Bookmarks: {_bookmark_list(document)}.")


def _group_matches(group: Mapping[str, Any], text: str) -> bool:
    return (
        group.get("name") == text or str(group.get("displayName", "")).casefold() == text.casefold()
    )


def _resolve_group(document: PbirDocument, ref: str) -> dict[str, Any]:
    text = ref.strip()
    found = [g for g in _group_items(document) if _group_matches(g, text)]
    if len(found) == 1:
        return found[0]
    if found:
        raise DocumentError(f"{len(found)} bookmark groups are named {text!r}; rename one first")
    groups = [f"'{g.get('displayName') or g.get('name')}'" for g in _group_items(document)]
    listed = join_names(groups, 8) if groups else "none yet"
    raise DocumentError(f"There is no bookmark group {text!r}. Groups: {listed}.")


def _container_of(document: PbirDocument, key: str) -> tuple[list[Any], int]:
    """The list that holds a bookmark's entry in bookmarks.json (the top level, or a group's
    children) and its index there; a bookmark missing from the list is appended first."""
    items = document.bookmark_items()
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("name") == key and "children" not in item:
            return items, index
        children = item.get("children") if isinstance(item, dict) else None
        if isinstance(children, list) and key in children:
            return children, children.index(key)
    items.append({"name": key})
    _touch_meta(document)
    return items, len(items) - 1


def _taken_bookmark_names(document: PbirDocument, besides: str | None = None) -> set[str]:
    return {
        str(b.get("displayName", "")).casefold()
        for k, b in document.bookmarks().items()
        if k != besides
    }


def _new_bookmark_id(document: PbirDocument) -> str:
    used = {k.removeprefix(BOOKMARK_PREFIX) for k in document.bookmarks()}
    used |= {str(g.get("name", "")).removeprefix(BOOKMARK_PREFIX) for g in _group_items(document)}
    return BOOKMARK_PREFIX + document.new_name(used)


# --- bookmarks: capturing a page --------------------------------------------------------------


def _filter_containers(holder: Mapping[str, Any]) -> dict[str, Any] | None:
    """A PBIR filterConfig as the exploration state's filter containers (legacy shape), sorted
    the way Desktop's capture sorts them: a filter hidden or locked in view mode is matched by
    its name (``byName``), one on a field by its expression (``byExpr``), an include or exclude
    filter by its type (``byType``), and anything else by its name."""
    config = holder.get("filterConfig")
    filters = config.get("filters") if isinstance(config, dict) else None
    by_name: dict[str, Any] = {}
    by_expr: list[dict[str, Any]] = []
    by_type: list[dict[str, Any]] = []
    for item in filters if isinstance(filters, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        container: dict[str, Any] = {"name": item["name"]}
        if isinstance(item.get("type"), str):
            container["type"] = item["type"]
        if isinstance(item.get("filter"), dict):
            container["filter"] = copy.deepcopy(item["filter"])
        if isinstance(item.get("field"), dict):
            container["expression"] = copy.deepcopy(item["field"])
        code = _HOW_CREATED_CODES.get(str(item.get("howCreated")))
        if code is not None:
            container["howCreated"] = code
        if item.get("isHiddenInViewMode") is True or item.get("isLockedInViewMode") is True:
            by_name[item["name"]] = container
        elif "expression" in container:
            by_expr.append(container)
        elif item.get("howCreated") in {"Include", "Exclude"}:
            by_type.append(container)
        else:
            by_name[item["name"]] = container
    found: dict[str, Any] = {}
    if by_name:
        found["byName"] = by_name
    if by_expr:
        found["byExpr"] = by_expr
    if by_type:
        found["byType"] = by_type
    return found or None


def _data_state(visual: dict[str, Any]) -> dict[str, Any]:
    """What a data-capturing bookmark remembers of one visual: slicer selection, sort, drill."""
    body = visual.get("visual") if isinstance(visual.get("visual"), dict) else {}
    assert isinstance(body, dict)
    state: dict[str, Any] = {}
    selections = [
        {"properties": {"filter": copy.deepcopy(e["properties"]["filter"])}}
        for e in _entries(body.get("objects"), "general")
        if isinstance(e.get("properties"), dict) and isinstance(e["properties"].get("filter"), dict)
    ]
    if selections:
        state["objects"] = {"merge": {"general": selections}}
    query = body.get("query") if isinstance(body.get("query"), dict) else {}
    assert isinstance(query, dict)
    sort = query.get("sortDefinition")
    if isinstance(sort, dict) and sort.get("isDefaultSort") is not True:
        order = [
            {
                "Direction": 1 if item.get("direction") == "Ascending" else 2,
                "Expression": copy.deepcopy(item["field"]),
            }
            for item in sort.get("sort") or []
            if isinstance(item, dict) and isinstance(item.get("field"), dict)
        ]
        if order:
            state["orderBy"] = order
    roles = query.get("queryState")
    active: dict[str, Any] = {}
    for role, role_state in roles.items() if isinstance(roles, dict) else []:
        projections = role_state.get("projections") if isinstance(role_state, dict) else None
        flagged = [p for p in projections or [] if isinstance(p, dict) and "active" in p]
        if flagged:
            active[role] = [copy.deepcopy(p["field"]) for p in flagged if p.get("active") is True]
    if active:
        state["activeProjections"] = active
    return state


def _group_tree(page: PageDoc, hidden: Callable[[str], bool]) -> dict[str, Any]:
    """visualContainerGroups: every group of the page, nested groups under ``children``. Each
    says ``isHidden`` true or false, as Desktop's capture does: applying a bookmark only
    changes a group whose state carries the flag."""

    def node(name: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"isHidden": hidden(name)}
        children = {
            child: node(child)
            for child, v in page.visuals.items()
            if is_group(v) and v.get("parentGroupName") == name
        }
        if children:
            entry["children"] = children
        return entry

    return {
        name: node(name)
        for name, visual in page.visuals.items()
        if is_group(visual)
        and not (
            isinstance(visual.get("parentGroupName"), str)
            and visual.get("parentGroupName") in page.visuals
        )
    }


def _capture(
    document: PbirDocument,
    page: PageDoc,
    *,
    data: bool,
    hide: set[str],
    show: set[str],
) -> dict[str, Any]:
    """The exploration state of a page as it is now, with ``hide``/``show`` applied in the
    bookmark only. Every visual is listed: applying a bookmark shows the visuals it does not
    list (Desktop applies it with clearUnmatchedData)."""

    def hidden(name: str) -> bool:
        if name in hide:
            return True
        if name in show:
            return False
        return page.visuals[name].get("isHidden") is True

    containers: dict[str, Any] = {}
    for name, visual in page.visuals.items():
        if is_group(visual):
            continue
        single: dict[str, Any] = {"visualType": visual_kind(visual) or "unknown"}
        entry: dict[str, Any] = {}
        if data:
            single.update(_data_state(visual))
            filters = _filter_containers(visual)
            if filters:
                entry["filters"] = filters
        if hidden(name):
            single["display"] = {"mode": "hidden"}
        entry["singleVisual"] = single
        containers[name] = entry
    section: dict[str, Any] = {}
    if data:
        filters = _filter_containers(page.data)
        if filters:
            section["filters"] = filters
    section["visualContainers"] = containers
    groups = _group_tree(page, hidden)
    if groups:
        section["visualContainerGroups"] = groups
    state: dict[str, Any] = {"version": EXPLORATION_VERSION, "activeSection": page.name}
    if data:
        filters = _filter_containers(document.report)
        if filters:
            state["filters"] = filters
    state["sections"] = {page.name: section}
    return state


def _options(
    *, data: bool, display: bool, current_page: bool, targets: Sequence[str]
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    if targets:
        options["applyOnlyToTargetVisuals"] = True
        options["targetVisualNames"] = list(targets)
    if not current_page:
        options["suppressActiveSection"] = True
    if not data:
        options["suppressData"] = True
    if not display:
        options["suppressDisplay"] = True
    return options


def _names_on(document: PbirDocument, page: PageDoc, refs: Sequence[str], what: str) -> list[str]:
    names: list[str] = []
    for ref in refs:
        found_page, visual = _find_visual(document, ref)
        if found_page is not page:
            raise DocumentError(
                f"{sentence(_label(visual, found_page))} is on page "
                f"{_page_label(found_page)}, and {what} is about page {_page_label(page)}"
            )
        name = _visual_name(visual)
        if name not in names:
            names.append(name)
    return names


def _active_page(document: PbirDocument) -> PageDoc:
    active = document.pages_meta.get("activePageName")
    if isinstance(active, str) and active in document.pages:
        return document.pages[active]
    return next(iter(document.pages.values()))


def _bookmark_page(document: PbirDocument, bookmark: Mapping[str, Any]) -> PageDoc:
    state = bookmark.get("explorationState")
    if isinstance(state, dict):
        active = state.get("activeSection")
        if isinstance(active, str) and active in document.pages:
            return document.pages[active]
        sections = state.get("sections")
        for name in sections if isinstance(sections, dict) else []:
            if name in document.pages:
                return document.pages[name]
    raise DocumentError(
        f"The bookmark {_bookmark_label(bookmark, '')} does not remember a page of this report"
    )


def _what_it_remembers(options: Mapping[str, Any], hidden: int) -> str:
    parts = []
    if not options.get("suppressDisplay"):
        parts.append(
            f"which visuals are shown ({hidden} hidden)" if hidden else "which visuals are shown"
        )
    if not options.get("suppressData"):
        parts.append("filters, slicers and sorts")
    if not options.get("suppressActiveSection"):
        parts.append("the page")
    remembered = _all(parts) if parts else "nothing (every option is off)"
    targets = options.get("targetVisualNames") if options.get("applyOnlyToTargetVisuals") else None
    if isinstance(targets, list) and targets:
        remembered += f", for {_plural(len(targets), 'selected visual')} only"
    return remembered


# Studio's view mode lists the report's bookmarks and applies one when it is picked or a
# button opens it (``render_navigation.bookmark_display``, ``bookmark_slicers``,
# ``bookmark_filters``).
_BOOKMARK_VISIBLE = (
    "Studio's view mode lists the bookmark and applies it; Power BI has it in View > Bookmarks."
)


def _bookmark_receipt(op: str) -> list[VisibleIn]:
    return [VisibleIn(op, "studio", _BOOKMARK_VISIBLE)]


def _apply_add_bookmark(document: PbirDocument, op: AddBookmark, model: ModelInfo) -> HandlerResult:
    name = op.name.strip()
    if not name:
        raise DocumentError("A bookmark needs a name")
    if name.casefold() in _taken_bookmark_names(document):
        raise DocumentError(f"There is already a bookmark named {name!r}")
    if not op.display and (op.hide or op.show):
        raise DocumentError(
            "Hiding or showing visuals in a bookmark needs Display on: the bookmark would not "
            "remember them otherwise"
        )
    page = resolve_page(document, op.page) if op.page else _active_page(document)
    targets = _names_on(document, page, op.visuals, "the bookmark")
    hide = set(_names_on(document, page, op.hide, "the bookmark"))
    show = set(_names_on(document, page, op.show, "the bookmark"))
    both = hide & show
    if both:
        raise DocumentError("A visual cannot be both hidden and shown in the same bookmark")
    group = _resolve_group(document, op.group) if op.group else None
    key = _new_bookmark_id(document)
    bookmark: dict[str, Any] = {"$schema": BOOKMARK_SCHEMA, "displayName": name, "name": key}
    options = _options(
        data=op.data, display=op.display, current_page=op.current_page, targets=targets
    )
    if options:
        bookmark["options"] = options
    bookmark["explorationState"] = _capture(document, page, data=op.data, hide=hide, show=show)
    document.add_bookmark(key, bookmark, group=None if group is None else str(group["name"]))
    summary = f"Added the bookmark {name!r} of page {_page_label(page)}"
    if group is not None:
        summary += f" in the group '{group.get('displayName')}'"
    summary += f"; it remembers {_what_it_remembers(options, _hidden_count(bookmark))}"
    return HandlerResult(summary + ".", visible_in=_bookmark_receipt(op.op))


def _hidden_count(bookmark: Mapping[str, Any]) -> int:
    """How many visuals the bookmark hides."""
    state = bookmark.get("explorationState")
    sections = state.get("sections") if isinstance(state, dict) else None
    count = 0
    for section in sections.values() if isinstance(sections, dict) else []:
        containers = section.get("visualContainers") if isinstance(section, dict) else None
        for entry in containers.values() if isinstance(containers, dict) else []:
            single = entry.get("singleVisual") if isinstance(entry, dict) else None
            display = single.get("display") if isinstance(single, dict) else None
            count += isinstance(display, dict) and display.get("mode") == "hidden"
    return count


def _section(bookmark: Mapping[str, Any], page: PageDoc) -> dict[str, Any] | None:
    state = bookmark.get("explorationState")
    sections = state.get("sections") if isinstance(state, dict) else None
    section = sections.get(page.name) if isinstance(sections, dict) else None
    return section if isinstance(section, dict) else None


def _display_state(bookmark: Mapping[str, Any], page: PageDoc) -> tuple[set[str], set[str]]:
    """(hidden, shown): the visuals and groups still on ``page`` that the bookmark's state
    hides, and the ones it shows outright (a listed visual without a display mode, a group
    with ``isHidden`` false). A group without the flag is in neither: applying the bookmark
    leaves it as it is."""
    hidden: set[str] = set()
    shown: set[str] = set()
    section = _section(bookmark, page)
    if section is None:
        return hidden, shown
    containers = section.get("visualContainers")
    for name, entry in containers.items() if isinstance(containers, dict) else []:
        if name not in page.visuals:
            continue
        single = entry.get("singleVisual") if isinstance(entry, dict) else None
        display = single.get("display") if isinstance(single, dict) else None
        is_hidden = isinstance(display, dict) and display.get("mode") == "hidden"
        (hidden if is_hidden else shown).add(name)

    def walk(tree: Any) -> None:
        for name, node in tree.items() if isinstance(tree, dict) else []:
            if not isinstance(node, dict):
                continue
            if name in page.visuals and node.get("isHidden") is True:
                hidden.add(name)
            elif name in page.visuals and node.get("isHidden") is False:
                shown.add(name)
            walk(node.get("children"))

    walk(section.get("visualContainerGroups"))
    return hidden, shown


def _hides(bookmark: Mapping[str, Any], page: PageDoc) -> set[str]:
    """What applying the bookmark hides on its page: nothing with Display off, and only its
    selected visuals (or what sits in them) when it applies to those alone."""
    options = bookmark.get("options")
    options = options if isinstance(options, dict) else {}
    if options.get("suppressDisplay"):
        return set()
    hidden, _ = _display_state(bookmark, page)
    if options.get("applyOnlyToTargetVisuals"):
        targets = {t for t in options.get("targetVisualNames") or [] if isinstance(t, str)}
        hidden = {
            n for n in hidden if n in targets or targets.intersection(_group_ancestors(page, n))
        }
    return hidden


def _merge_data(state: dict[str, Any], fresh: Mapping[str, Any], page_name: str) -> None:
    """Put the data half of a fresh capture (filters, slicer selections, sorts, drill) into a
    bookmark's state, keeping what the state says about display and everything else."""
    if "filters" in fresh:
        state["filters"] = fresh["filters"]
    sections = state.get("sections")
    if not isinstance(sections, dict):
        sections = state["sections"] = {}
    section = sections.get(page_name)
    if not isinstance(section, dict):
        section = sections[page_name] = {}
    new_section = fresh["sections"][page_name]
    if "filters" in new_section:
        section["filters"] = new_section["filters"]
    containers = section.get("visualContainers")
    if not isinstance(containers, dict):
        containers = section["visualContainers"] = {}
    for name, entry in new_section["visualContainers"].items():
        old = containers.get(name)
        if not isinstance(old, dict):
            containers[name] = entry
            continue
        if "filters" in entry:
            old["filters"] = entry["filters"]
        single = old.get("singleVisual")
        if not isinstance(single, dict):
            single = old["singleVisual"] = {"visualType": entry["singleVisual"]["visualType"]}
        for data_key in ("orderBy", "activeProjections"):
            if data_key in entry["singleVisual"]:
                single[data_key] = entry["singleVisual"][data_key]
        selection = entry["singleVisual"].get("objects")
        if selection:  # a slicer's selection; other merged formatting the state has stays
            objects = single.get("objects")
            if not isinstance(objects, dict):
                objects = single["objects"] = {}
            merge = objects.get("merge")
            if not isinstance(merge, dict):
                merge = objects["merge"] = {}
            merge["general"] = selection["merge"]["general"]


def _apply_update_bookmark(
    document: PbirDocument, op: UpdateBookmark, model: ModelInfo
) -> HandlerResult:
    key = _resolve_bookmark(document, op.bookmark)
    bookmark = document.bookmark(key)
    page = _bookmark_page(document, bookmark)
    old = bookmark.get("options") if isinstance(bookmark.get("options"), dict) else {}
    assert isinstance(old, dict)
    data = (not old.get("suppressData")) if op.data is None else op.data
    display = (not old.get("suppressDisplay")) if op.display is None else op.display
    current_page = (
        (not old.get("suppressActiveSection")) if op.current_page is None else op.current_page
    )
    if op.visuals is None:
        kept = old.get("targetVisualNames") if old.get("applyOnlyToTargetVisuals") else None
        targets = [t for t in kept or [] if isinstance(t, str) and t in page.visuals]
    else:
        targets = _names_on(document, page, op.visuals, "the bookmark")
    notes: list[str] = []
    hid_before = _hides(bookmark, page)
    # What the bookmark itself hides or shows is Studio's only record of it (the page does not
    # hold it), so a recapture keeps it for every visual and group still on the page; the rest
    # is taken from the page as it is now.
    hidden, shown = _display_state(bookmark, page)
    # A bookmark that starts remembering data needs the data captured now: an old state holds
    # none, and Desktop resets every filter a data bookmark does not list. Only the data half is
    # taken when no recapture was asked for.
    newly_data = data and bool(old.get("suppressData"))
    options = _options(data=data, display=display, current_page=current_page, targets=targets)
    if options:
        bookmark["options"] = options
    else:
        bookmark.pop("options", None)
    if op.recapture:
        bookmark["explorationState"] = _capture(document, page, data=data, hide=hidden, show=shown)
    elif newly_data:
        fresh = _capture(document, page, data=True, hide=hidden, show=shown)
        state = bookmark.get("explorationState")
        if isinstance(state, dict):
            _merge_data(state, fresh, page.name)
        else:
            bookmark["explorationState"] = fresh
        notes.append(
            "the page's current filters, slicers and sorts were captured, since it now "
            "remembers data"
        )
    document.touch_bookmark(key)
    label = _bookmark_label(bookmark, key)
    summary = (
        f"Updated the bookmark {label} from page {_page_label(page)} as it is now (the "
        "visuals it hid or showed keep that)"
        if op.recapture
        else f"Changed what the bookmark {label} remembers"
    )
    summary += f"; it remembers {_what_it_remembers(options, _hidden_count(bookmark))}."
    if notes:
        summary += " " + sentence(notes[0]) + "."
    warnings: list[str] = []
    shown_now = sorted(hid_before - _hides(bookmark, page))
    if shown_now:
        labels = [_label(page.visuals[n], page) for n in shown_now]
        warnings.append(f"The bookmark no longer hides {_all(labels)}.")
    return HandlerResult(summary, warnings=warnings, visible_in=_bookmark_receipt(op.op))


def _apply_rename_bookmark(
    document: PbirDocument, op: RenameBookmark, model: ModelInfo
) -> HandlerResult:
    key = _resolve_bookmark(document, op.bookmark)
    bookmark = document.bookmark(key)
    name = op.name.strip()
    if not name:
        raise DocumentError("A bookmark needs a name")
    if name.casefold() in _taken_bookmark_names(document, besides=key):
        raise DocumentError(f"There is already a bookmark named {name!r}")
    old = str(bookmark.get("displayName") or key)
    bookmark["displayName"] = name
    document.touch_bookmark(key)
    # Buttons and navigators point at the id, which stays.
    return HandlerResult(
        f"Renamed the bookmark {old!r} to {name!r}.",
        visible_in=_bookmark_receipt(op.op),
    )


def _apply_move_bookmark(
    document: PbirDocument, op: MoveBookmark, model: ModelInfo
) -> HandlerResult:
    key = _resolve_bookmark(document, op.bookmark)
    label = _bookmark_label(document.bookmark(key), key)
    if op.group is not None and op.top_level:
        raise DocumentError("Say either which group to move the bookmark into, or top level")
    if op.group is None and not op.top_level and op.position is None:
        raise DocumentError("Say where to move the bookmark: a position, a group, or top level")
    target_group = _resolve_group(document, op.group) if op.group is not None else None
    container, index = _container_of(document, key)
    top = document.bookmark_items()
    unlisted = Unlisted((), ())
    if target_group is None and not op.top_level:
        # A new position in the same list.
        assert op.position is not None
        entry = container.pop(index)
        container.insert(min(op.position - 1, len(container)), entry)
        where = ""
    else:
        if target_group is not None and container is target_group["children"]:
            raise DocumentError(
                f"The bookmark {label} is already in the group '{target_group.get('displayName')}'"
            )
        if target_group is None and container is top:
            raise DocumentError(f"The bookmark {label} is not in a group")
        # Out of its list (a group left empty goes, as Desktop removes empty groups, and the
        # navigators on it list every bookmark), into the target list.
        unlisted = document.unlist_bookmark(key)
        destination: list[Any] = (
            target_group["children"] if target_group is not None else document.bookmark_items()
        )
        item: Any = key if target_group is not None else {"name": key}
        spot = len(destination) if op.position is None else min(op.position - 1, len(destination))
        destination.insert(spot, item)
        where = (
            f" into the group '{target_group.get('displayName')}'"
            if target_group is not None
            else " out of its group"
        )
    _touch_meta(document)
    _, final_index = _container_of(document, key)
    notes, warnings = _released(document, unlisted)
    summary = f"Moved the bookmark {label}{where} to position {final_index + 1}"
    summary += "".join(f"; {note}" for note in notes)
    return HandlerResult(summary + ".", warnings=warnings, visible_in=_bookmark_receipt(op.op))


def _apply_group_bookmarks(
    document: PbirDocument, op: GroupBookmarks, model: ModelInfo
) -> HandlerResult:
    name = op.name.strip()
    if not name:
        raise DocumentError("A bookmark group needs a name")
    if any(
        str(g.get("displayName", "")).casefold() == name.casefold() for g in _group_items(document)
    ):
        raise DocumentError(f"There is already a bookmark group named {name!r}")
    keys: list[str] = []
    for ref in op.bookmarks:
        key = _resolve_bookmark(document, ref)
        if key not in keys:
            keys.append(key)
    # The group takes the place of the first member at the top level (or of its old group).
    container, index = _container_of(document, keys[0])
    top = document.bookmark_items()
    anchor = (
        index
        if container is top
        else next(
            i
            for i, item in enumerate(top)
            if isinstance(item, dict) and item.get("children") is container
        )
    )
    before = [id(item) for item in top[:anchor]]
    unlisted = Unlisted((), ())
    for key in keys:
        unlisted = _merge_unlisted(unlisted, document.unlist_bookmark(key))
    items = document.bookmark_items()
    position = sum(1 for item in items if id(item) in before)
    group_id = _new_bookmark_id(document)
    items.insert(position, {"name": group_id, "displayName": name, "children": keys})
    _touch_meta(document)
    labels = [_bookmark_label(document.bookmark(k), k) for k in keys]
    notes, warnings = _released(document, unlisted)
    summary = f"Grouped {join_names(labels)} as {name!r}"
    summary += "".join(f"; {note}" for note in notes)
    return HandlerResult(summary + ".", warnings=warnings, visible_in=_bookmark_receipt(op.op))


def _apply_ungroup_bookmarks(
    document: PbirDocument, op: UngroupBookmarks, model: ModelInfo
) -> HandlerResult:
    group = _resolve_group(document, op.group)
    items = document.bookmark_items()
    index = next(i for i, item in enumerate(items) if item is group)
    children = [c for c in group["children"] if isinstance(c, str)]
    items[index : index + 1] = [{"name": c} for c in children]
    _touch_meta(document)
    released = document.release_bookmark_groups([str(group.get("name"))])
    _, warnings = _released(document, Unlisted((), tuple(released)))
    return HandlerResult(
        f"Ungrouped '{group.get('displayName')}': its {_plural(len(children), 'bookmark')} "
        "stay in its place.",
        warnings=warnings,
        visible_in=_bookmark_receipt(op.op),
    )


def _reference_label(
    document: PbirDocument, reference: BookmarkReference, form: Literal["use", "cleared"]
) -> str:
    """A reference to a bookmark as this family names visuals: what it does ("use"), or what
    deleting the bookmark changed about it ("cleared")."""
    page = document.pages[reference.page]
    who = f"{_label(page.visuals[reference.visual], page)} on page {_page_label(page)}"
    if form == "use":
        words = {
            "button": "opens it",
            "navigator_deselection": "opens it on deselection",
            "navigator_group": "lists it",
        }.get(reference.kind, "uses it")
        return f"{sentence(who)} {words}"
    if reference.kind == "button":
        return f"Switched off the action of {who}."
    if reference.kind == "navigator_deselection":
        return f"{sentence(who)} no longer opens a bookmark on deselection."
    return f"{sentence(who)} lists every bookmark again."


def _apply_delete_bookmark(
    document: PbirDocument, op: DeleteBookmark, model: ModelInfo
) -> HandlerResult:
    key = _resolve_bookmark(document, op.bookmark)
    bookmark = document.bookmark(key)
    label = _bookmark_label(bookmark, key)
    # What points at a bookmark is indexed by report_refs, which also cleans it up when a page
    # delete takes bookmarks with it.
    references = bookmark_references(document, key)
    changes: list[str] = []
    if references:
        if not op.cleanup_references:
            uses = [_reference_label(document, r, "use") for r in references]
            raise NeedsConfirmation(
                "remove_bookmark_references",
                f"The bookmark {label} is still used: {_listed(uses)}. "
                f"Deleting it switches {'that' if len(references) == 1 else 'those'} off.",
            )
        changes = [_reference_label(document, r, "cleared") for r in references]
        clear_bookmark_references(document, key, str(bookmark.get("displayName") or key))
    # Its entry in bookmarks.json goes too; a group it leaves empty goes, and the navigators on
    # that group list every bookmark.
    unlisted = document.remove_bookmark(key)
    notes, warnings = _released(document, unlisted)
    summary = f"Deleted the bookmark {label}"
    summary += "".join(f"; {note}" for note in notes) + "."
    if changes:
        summary += " " + " ".join(changes)
    return HandlerResult(summary, warnings=warnings, visible_in=_bookmark_receipt(op.op))


def _apply_set_bookmark_visual_hidden(
    document: PbirDocument, op: SetBookmarkVisualHidden, model: ModelInfo
) -> HandlerResult:
    key = _resolve_bookmark(document, op.bookmark)
    bookmark = document.bookmark(key)
    label = _bookmark_label(bookmark, key)
    page, visual = _find_visual(document, op.visual)
    name = _visual_name(visual)
    options = bookmark.get("options") if isinstance(bookmark.get("options"), dict) else {}
    assert isinstance(options, dict)
    if options.get("suppressDisplay"):
        raise DocumentError(
            f"The bookmark {label} does not remember which visuals are shown (Display is off); "
            "turn Display on for it first"
        )
    state = bookmark.get("explorationState")
    sections = state.get("sections") if isinstance(state, dict) else None
    section = sections.get(page.name) if isinstance(sections, dict) else None
    if not isinstance(section, dict):
        raise DocumentError(
            f"The bookmark {label} is about another page than {_label(visual, page)} "
            f"(page {_page_label(page)})"
        )
    warnings: list[str] = []
    if options.get("applyOnlyToTargetVisuals"):
        targets = options.setdefault("targetVisualNames", [])
        if name not in targets:
            targets.append(name)
            warnings.append(
                f"The bookmark applies to selected visuals only; {_label(visual, page)} was "
                "added to them."
            )
    if is_group(visual):
        tree = section.setdefault("visualContainerGroups", {})
        path = [name, *_group_ancestors(page, name)][::-1]
        node: dict[str, Any] = tree
        for step, group_name in enumerate(path):
            holder = node if step == 0 else node.setdefault("children", {})
            node = holder.setdefault(group_name, {})
        # False is written, not left out: applying a bookmark leaves a group without the flag
        # as it is on the page.
        node["isHidden"] = op.hidden
    else:
        containers = section.setdefault("visualContainers", {})
        entry = containers.setdefault(name, {})
        single = entry.setdefault("singleVisual", {"visualType": visual_kind(visual) or "unknown"})
        if op.hidden:
            single["display"] = {"mode": "hidden"}
        else:
            single.pop("display", None)
    if not op.hidden:
        hidden, _ = _display_state(bookmark, page)
        around = [g for g in _group_ancestors(page, name) if g in hidden]
        if around:
            group = page.visuals[around[-1]]
            warnings.append(
                f"{sentence(_label(visual, page))} sits in {_label(group, page)}, which "
                "the bookmark hides, so it stays out of sight until that group is shown too."
            )
    document.touch_bookmark(key)
    verb = "hides" if op.hidden else "shows"
    return HandlerResult(
        f"The bookmark {label} now {verb} {_label(visual, page)}; the page itself is unchanged.",
        warnings=warnings,
        visible_in=_bookmark_receipt(op.op),
    )


def _group_ancestors(page: PageDoc, name: str) -> list[str]:
    found: list[str] = []
    parent = page.visuals[name].get("parentGroupName")
    while isinstance(parent, str) and parent in page.visuals and parent not in found:
        found.append(parent)
        parent = page.visuals[parent].get("parentGroupName")
    return found


# --- buttons and navigators ------------------------------------------------------------------


_BUTTON_VISIBLE = (
    "Studio's canvas does not draw buttons, navigators or what they do; check them in Power BI."
)


def _is_drillthrough_page(page: PageDoc) -> bool:
    return _page_type(page) == "Drillthrough"


def _check_url(url: str) -> str:
    text = url.strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise DocumentError(
            f"{text!r} is not a web address; a web link starts with https:// (or http://)"
        )
    return text


def _action_properties(
    document: PbirDocument, action: ButtonAction, visual_type: str
) -> tuple[dict[str, Any], str]:
    """visualLink properties for an action and a plain description of it."""
    props: dict[str, Any] = {"show": expr.boolean(True)}
    kind = action.type
    if kind == "none":
        return {"show": expr.boolean(False)}, "no action"
    if kind == "back":
        props["type"] = expr.string("Back")
        return props, "go back to the previous page"
    if kind == "qna":
        props["type"] = expr.string("Qna")
        return props, "open Q&A"
    if kind == "bookmark":
        if not action.bookmark:
            raise DocumentError("Say which bookmark the button opens")
        key = _resolve_bookmark(document, action.bookmark)
        props["type"] = expr.string("Bookmark")
        props["bookmark"] = expr.string(key)
        return props, f"open the bookmark {_bookmark_label(document.bookmark(key), key)}"
    if kind in {"page", "drillthrough"}:
        if not action.page:
            raise DocumentError(
                "Say which page the button goes to"
                if kind == "page"
                else "Say which drillthrough page the button opens"
            )
        page = resolve_page(document, action.page)
        if kind == "page":
            props["type"] = expr.string("PageNavigation")
            props["navigationSection"] = expr.string(page.name)
            return props, f"go to page {_page_label(page)}"
        if visual_type in _NO_DRILLTHROUGH_LINK:
            raise DocumentError(
                "Only a button or a shape can drill through; add a button for it instead"
            )
        if not _is_drillthrough_page(page):
            raise DocumentError(
                f"Page {_page_label(page)} is not a drillthrough page; make it one first "
                "(set its drillthrough fields)"
            )
        props["type"] = expr.string("Drillthrough")
        props["drillthroughSection"] = expr.string(page.name)
        return props, f"drill through to page {_page_label(page)}"
    if not action.url:
        raise DocumentError("Say which web address the button opens")
    props["type"] = expr.string("WebUrl")
    props["webUrl"] = expr.string(_check_url(action.url))
    return props, f"open {action.url.strip()}"


# The destination properties of an action: a new action drops the old one's, so a page or
# bookmark the button no longer opens is not left referenced.
_DESTINATIONS = ("bookmark", "navigationSection", "drillthroughSection", "webUrl", "qna")


def _write_action(
    visual: dict[str, Any], props: Mapping[str, Any] | None, tooltip: str | None
) -> None:
    """Replace the visual's action with ``props`` (None keeps it) and set its tooltip."""
    entry = _general_entry(_container_objects(visual), "visualLink")
    current = _props(entry)
    if props is not None:
        for key in _DESTINATIONS:
            current.pop(key, None)
        if _literal(props.get("show")) is False:
            current.pop("type", None)
        current.update(copy.deepcopy(dict(props)))
    if tooltip is not None:
        if tooltip.strip():
            current["tooltip"] = expr.string(tooltip.strip())
        else:
            current.pop("tooltip", None)


def _has_action(visual: Mapping[str, Any]) -> bool:
    """True when the visual's action is on and has a type. Desktop reads an action that is on
    without a type as Back, so a tooltip is never written on its own."""
    body = visual.get("visual")
    holder = body.get("visualContainerObjects") if isinstance(body, dict) else None
    for entry in _entries(holder, "visualLink"):
        props = entry.get("properties")
        if (
            isinstance(props, dict)
            and _literal(props.get("show")) is True
            and isinstance(_literal(props.get("type")), str)
        ):
            return True
    return False


_NEEDS_ACTION = "A button's tooltip shows with its action; say what clicking it does as well"


# The actions Studio's view mode follows when a button is clicked (``render_navigation.
# action_of``: go back, open a bookmark, go to a page, drill through); a web address and Q&A
# open in Power BI only. A navigator's buttons go to their page or bookmark in both.
_FOLLOWED_ACTIONS = frozenset({"back", "bookmark", "page", "drillthrough", "navigator"})
_ACTION_FOLLOWED = "Clicking it in Studio's view mode does the same."
_ACTION_VISIBLE = "Clicking it works in Power BI."


def _drawn(
    op: str, visual_type: str, written: Sequence[tuple[str, str]] = (), action: str | None = None
) -> list[VisibleIn]:
    """Where a new or restyled button or navigator shows: Power BI only until Studio's
    renderer draws the type (``report_tokens.family_of``) and every property written
    (``property_is_rendered``). ``action`` is what clicking it does (an ``ActionType``, or
    "navigator"), which the receipt names."""
    drawn = family_of(visual_type) != "unsupported" and all(
        property_is_rendered(visual_type, obj, prop, container=False) for obj, prop in written
    )
    if not drawn:
        return _powerbi_only(op, _BUTTON_VISIBLE)
    if action in _FOLLOWED_ACTIONS:
        return [VisibleIn(op, "studio", _ACTION_FOLLOWED)]
    if action is None or action == "none":
        return [VisibleIn(op, "studio")]
    return [VisibleIn(op, "studio", "What clicking it does happens in Power BI.")]


def _action_receipt(op: str, action: str | None) -> list[VisibleIn]:
    """Where a new action shows: Studio's view mode follows the ones it knows."""
    if action in _FOLLOWED_ACTIONS or action == "none":
        return [VisibleIn(op, "studio", _ACTION_FOLLOWED if action != "none" else "")]
    return _powerbi_only(op, _ACTION_VISIBLE)


def _apply_add_button(document: PbirDocument, op: AddButton, model: ModelInfo) -> HandlerResult:
    page = resolve_page(document, op.page)
    slicers = _SLICER_BUTTONS.get(op.kind)
    if slicers is not None and op.action is not None:
        article = "An" if slicers[1].startswith("A") else "A"
        raise DocumentError(
            f"{article} '{slicers[1]}' button only does that; add a blank button for another action"
        )
    size = _SLICER_BUTTON_SIZE if slicers is not None else _BUTTON_SIZE
    width = op.width if op.width is not None else size[0]
    height = op.height if op.height is not None else size[1]
    placement = choose_placement(page, width, height, op.x, op.y)
    visual = new_visual(document, page, "actionButton", placement)
    objects = _own_objects(visual)
    objects["icon"] = [
        {
            "properties": {"shapeType": expr.string(_BUTTON_ICONS[op.kind])},
            "selector": {"id": "default"},
        }
    ]
    written = [("icon", "shapeType")]
    words = op.text or (slicers[1] if slicers is not None else None)
    if slicers is not None:
        # Desktop's slicer buttons: the icon off, the words centred.
        objects["icon"].append({"properties": {"show": expr.boolean(False)}})
        written.append(("icon", "show"))
    if words:
        # Desktop's own text defaults: the switch without a selector, the words per state.
        text_props = {"text": expr.string(words)}
        if slicers is not None:
            text_props["horizontalAlignment"] = expr.string("center")
            written.append(("text", "horizontalAlignment"))
        objects["text"] = [
            {"properties": {"show": expr.boolean(True)}},
            {"properties": text_props, "selector": {"id": "default"}},
        ]
        written.append(("text", "text"))
    what = ""
    action = op.action
    if slicers is not None:
        action_props: dict[str, Any] | None = {
            "show": expr.boolean(True),
            "type": expr.string(slicers[0]),
            "tooltipPlaceholderText": expr.string(slicers[2]),
        }
        what = f"{slicers[1][0].lower()}{slicers[1][1:]} on the page"
    elif action is None and op.kind in _DEFAULT_ACTIONS:
        action_props = {
            "show": expr.boolean(True),
            "type": expr.string(_DEFAULT_ACTIONS[op.kind]),
        }
        what = {"back": "go back", "bookmark": "open a bookmark", "qna": "open Q&A"}[op.kind]
    elif action is not None:
        action_props, what = _action_properties(document, action, "actionButton")
    else:
        action_props = None
    if op.tooltip and (action_props is None or _literal(action_props.get("show")) is False):
        raise DocumentError(_NEEDS_ACTION)
    if action_props is not None:
        _write_action(visual, action_props, op.tooltip)
    attach(document, page, visual)
    kind_word = {
        "qna": "Q&A",
        "left_arrow": "left arrow",
        "right_arrow": "right arrow",
        "apply_all_slicers": "'Apply all slicers'",
        "clear_all_slicers": "'Clear all slicers'",
    }.get(op.kind, op.kind)
    article = "an" if kind_word.strip("'")[:1].lower() in "aeiou" else "a"
    where = f"({tidy(placement.x)}, {tidy(placement.y)})"
    summary = f"Added {article} {kind_word} button to page {_page_label(page)} at {where}"
    if op.text:
        summary += f" saying {op.text!r}"
    if what:
        summary += f"; clicking it will {what}"
    warnings: list[str] = []
    if op.kind == "bookmark" and action is None:
        warnings.append("The button does not open a bookmark yet; say which one it should open.")
    if placement.crowded:
        warnings.append(
            "There is no free space left on the page, so it was put where it covers the least."
        )
    # A bookmark button without its bookmark does nothing yet, in Studio or in Power BI.
    clicked = action.type if action is not None else {"back": "back", "qna": "qna"}.get(op.kind)
    if slicers is not None:
        clicked = op.kind  # applying or clearing the slicers happens in Power BI
    return HandlerResult(
        summary + ".",
        warnings=warnings,
        visible_in=_drawn(op.op, "actionButton", written, clicked),
    )


def _apply_set_button_action(
    document: PbirDocument, op: SetButtonAction, model: ModelInfo
) -> HandlerResult:
    page, visual = _leaf(document, op.visual)
    kind = visual_kind(visual)
    if kind not in _LINKABLE:
        raise DocumentError(
            f"{sentence(_label(visual, page))} cannot have an action; only buttons, shapes and "
            "pictures can"
        )
    if op.action is None and op.tooltip is None:
        raise DocumentError("Say what the button should do, or its tooltip")
    what = ""
    props: dict[str, Any] | None = None
    if op.action is not None:
        props, what = _action_properties(document, op.action, kind)
        if op.tooltip and op.action.type == "none":
            raise DocumentError(_NEEDS_ACTION)
    elif op.tooltip and not _has_action(visual):
        raise DocumentError(_NEEDS_ACTION)
    _write_action(visual, props, op.tooltip)
    document.touch_visual(page, _visual_name(visual))
    label = _label(visual, page)
    parts = []
    if op.action is not None:
        parts.append(
            f"clicking {label} will now {what}"
            if op.action.type != "none"
            else f"{label} has no action now"
        )
    if op.tooltip is not None:
        parts.append(
            f"its tooltip says {op.tooltip.strip()!r}"
            if op.tooltip.strip()
            else "its tooltip was removed"
        )
    # A tooltip alone: Power BI shows it on hover; Studio's receipt does not promise it.
    clicked = op.action.type if op.action is not None else None
    return HandlerResult(
        sentence("; ".join(parts)) + ".", visible_in=_action_receipt(op.op, clicked)
    )


def _navigator_options(
    document: PbirDocument,
    visual: dict[str, Any],
    kind: str,
    *,
    orientation: str | None,
    rows: int | None,
    columns: int | None,
    bookmark_group: str | None,
    show_hidden_pages: bool | None,
    show_tooltip_pages: bool | None,
    show_pages: Sequence[str] = (),
    hide_pages: Sequence[str] = (),
    deselection_bookmark: str | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Write a navigator's options; returns what changed in words and the (object, property)
    pairs written."""
    objects = _own_objects(visual)
    changed: list[str] = []
    if (rows is not None or columns is not None) and orientation not in {None, "grid"}:
        raise DocumentError("Rows and columns only apply to a grid layout")
    if rows is not None or columns is not None:
        orientation = "grid"
    if orientation is not None:
        layout = _props(_general_entry(objects, "layout"))
        layout["orientation"] = _encode(
            kind, "layout", "orientation", str(_ORIENTATIONS[orientation])
        )
        if orientation == "grid":
            for key, value in (("rowCount", rows), ("columnCount", columns)):
                if value is not None:
                    layout[key] = _encode(kind, "layout", key, value)
        else:
            layout.pop("rowCount", None)
            layout.pop("columnCount", None)
        changed.append(
            f"{orientation} layout"
            + (
                f" ({rows or '?'} x {columns or '?'})"
                if orientation == "grid" and (rows or columns)
                else ""
            )
        )
    if bookmark_group is not None:
        if kind != "bookmarkNavigator":
            raise DocumentError("Only a bookmark navigator lists a bookmark group")
        if bookmark_group.strip():
            group = _resolve_group(document, bookmark_group)
            group_id = str(group["name"])
            changed.append(f"the bookmarks of the group '{group.get('displayName')}'")
        else:
            group_id = ""  # Desktop's "All"
            changed.append("every bookmark")
        _props(_general_entry(objects, "bookmarks"))["bookmarkGroup"] = expr.string(group_id)
    if deselection_bookmark is not None:
        if kind != "bookmarkNavigator":
            raise DocumentError("Only a bookmark navigator opens a bookmark on deselection")
        props = _props(_general_entry(objects, "bookmarks"))
        if deselection_bookmark.strip():
            key = _resolve_bookmark(document, deselection_bookmark)
            props["allowDeselectionBookmark"] = expr.boolean(True)
            props["deselectionBookmark"] = expr.string(key)
            label = _bookmark_label(document.bookmark(key), key)
            changed.append(f"clicking the selected bookmark again opens {label}")
        else:
            props.pop("deselectionBookmark", None)
            props["allowDeselectionBookmark"] = expr.boolean(False)
            changed.append("no bookmark on deselection")
    page_options = [
        (k, v)
        for k, v in (
            ("showHiddenPages", show_hidden_pages),
            ("showTooltipPages", show_tooltip_pages),
        )
        if v is not None
    ]
    if (page_options or show_pages or hide_pages) and kind != "pageNavigator":
        raise DocumentError("Only a page navigator lists pages")
    for key, flag in page_options:
        assert flag is not None
        _props(_general_entry(objects, "pages"))[key] = expr.boolean(flag)
        what = "hidden pages" if key == "showHiddenPages" else "tooltip pages"
        changed.append(f"{'shows' if flag else 'leaves out'} {what}")
    both = {resolve_page(document, p).name for p in show_pages} & {
        resolve_page(document, p).name for p in hide_pages
    }
    if both:
        raise DocumentError("A page cannot be both shown and left out of the navigator")
    for refs, flag in ((show_pages, True), (hide_pages, False)):
        for ref in refs:
            page = resolve_page(document, ref)
            expr.set_property(objects, "pages", "showPage", expr.boolean(flag), {"id": page.name})
            changed.append(f"{'lists' if flag else 'leaves out'} page {_page_label(page)}")
    written = [
        (name, prop)
        for name in ("layout", "bookmarks", "pages")
        for entry in _entries(objects, name)
        for prop in entry.get("properties") or {}
    ]
    return changed, list(dict.fromkeys(written))


def _apply_add_navigator(
    document: PbirDocument, op: AddNavigator, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    kind = "pageNavigator" if op.kind == "page" else "bookmarkNavigator"
    default_width = (
        min(max(200.0, 200.0 * len(document.pages)), 1000.0) if op.kind == "page" else 600.0
    )
    width = op.width if op.width is not None else default_width
    height = op.height if op.height is not None else _NAVIGATOR_HEIGHT
    placement = choose_placement(page, width, height, op.x, op.y)
    visual = new_visual(document, page, kind, placement)
    options, written = _navigator_options(
        document,
        visual,
        kind,
        orientation=op.orientation,
        rows=op.rows,
        columns=op.columns,
        bookmark_group=op.bookmark_group,
        show_hidden_pages=op.show_hidden_pages,
        show_tooltip_pages=op.show_tooltip_pages,
    )
    if not visual["visual"].get("objects"):
        visual["visual"].pop("objects", None)
    attach(document, page, visual)
    what = "page navigator" if op.kind == "page" else "bookmark navigator"
    summary = (
        f"Added a {what} to page {_page_label(page)} at ({tidy(placement.x)}, {tidy(placement.y)})"
    )
    if options:
        summary += f" ({_all(options)})"
    warnings = []
    if op.kind == "bookmark" and not document.bookmarks():
        warnings.append(
            "The report has no bookmarks yet, so the navigator is empty until you add some."
        )
    if placement.crowded:
        warnings.append(
            "There is no free space left on the page, so it was put where it covers the least."
        )
    return HandlerResult(
        summary + ".", warnings=warnings, visible_in=_drawn(op.op, kind, written, "navigator")
    )


def _apply_set_navigator(
    document: PbirDocument, op: SetNavigator, model: ModelInfo
) -> HandlerResult:
    page, visual = _leaf(document, op.visual)
    kind = visual_kind(visual)
    if kind not in {"pageNavigator", "bookmarkNavigator"}:
        raise DocumentError(f"{sentence(_label(visual, page))} is not a page or bookmark navigator")
    changed, written = _navigator_options(
        document,
        visual,
        kind,
        orientation=op.orientation,
        rows=op.rows,
        columns=op.columns,
        bookmark_group=op.bookmark_group,
        show_hidden_pages=op.show_hidden_pages,
        show_tooltip_pages=op.show_tooltip_pages,
        show_pages=op.show_pages,
        hide_pages=op.hide_pages,
        deselection_bookmark=op.deselection_bookmark,
    )
    if not changed:
        raise DocumentError(
            "Say what to change: layout, bookmark group, deselection bookmark or pages"
        )
    document.touch_visual(page, _visual_name(visual))
    return HandlerResult(
        f"Set {_label(visual, page)} to: {_all(changed)}.",
        visible_in=_drawn(op.op, kind, written, "navigator"),
    )


# --- drillthrough and tooltip pages ------------------------------------------------------------


_PAGE_VISIBLE = "Studio's canvas does not drill through or show page tooltips; try it in Power BI."


@dataclass(frozen=True)
class _Column:
    table: str
    name: str

    @property
    def expression(self) -> dict[str, Any]:
        return expr.column_ref(self.table, self.name)

    @property
    def text(self) -> str:
        return f"{self.table}[{self.name}]"


def _resolve_columns(
    model: ModelInfo, fields: Sequence[NavField], why: str
) -> tuple[list[_Column], list[str]]:
    """The columns in the model's spelling; with no model to check against (an empty one) the
    names are taken as given, with a warning."""
    unchecked = not model.columns and not model.measures
    found: list[_Column] = []
    warnings: list[str] = []
    for ref in fields:
        if unchecked:
            if ref.table is None:
                raise DocumentError(f"Say which table {ref.name!r} is in")
            column = _Column(ref.table, ref.name)
        else:
            kind, table, name = model.resolve(ref)
            if kind != "column":
                raise DocumentError(f"{name} is a measure; {why} needs a column")
            column = _Column(table, name)
        if column not in found:
            found.append(column)
    if unchecked and found:
        warnings.append(
            "Studio could not check the fields against the data model; check them in Power BI."
        )
    return found, warnings


def _field_column(node: Any) -> _Column | None:
    """The column a bound field expression names (``{"Column": {SourceRef.Entity, Property}}``)."""
    column = node.get("Column") if isinstance(node, dict) else None
    if not isinstance(column, dict):
        return None
    source = column.get("Expression", {}).get("SourceRef", {})
    entity = source.get("Entity") if isinstance(source, dict) else None
    prop = column.get("Property")
    if isinstance(entity, str) and isinstance(prop, str):
        return _Column(entity, prop)
    return None


def _key(column: _Column) -> tuple[str, str]:
    return column.table.casefold(), column.name.casefold()


def _page_filters(page: PageDoc) -> list[dict[str, Any]]:
    config = page.data.setdefault("filterConfig", {})
    filters: list[dict[str, Any]] = config.setdefault("filters", [])
    return filters


def _used_filter_names(document: PbirDocument) -> set[str]:
    used = collect_filter_names(document)
    for bookmark in document.bookmarks().values():
        state = bookmark.get("explorationState")
        stack: list[Any] = [state]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                if isinstance(node.get("name"), str) and str(node["name"]).startswith("Filter"):
                    used.add(str(node["name"]))
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)
    return used


def _new_filter_name(document: PbirDocument, used: set[str]) -> str:
    name = "Filter" + document.new_name({n.removeprefix("Filter") for n in used})
    used.add(name)
    return name


def _binding_names(document: PbirDocument) -> set[str]:
    used: set[str] = set()
    for page in document.pages.values():
        binding = page.data.get("pageBinding")
        if isinstance(binding, dict):
            if isinstance(binding.get("name"), str):
                used.add(binding["name"])
            for param in binding.get("parameters") or []:
                if isinstance(param, dict) and isinstance(param.get("name"), str):
                    used.add(param["name"])
    return used


def _bound_fields(page: PageDoc) -> dict[tuple[str, str], tuple[_Column, dict[str, Any]]]:
    """The binding's parameters by the column they are for (their own fieldExpr, else the field
    of the filter they are bound to), in order."""
    binding = page.data.get("pageBinding")
    params = binding.get("parameters") if isinstance(binding, dict) else None
    filters = {f.get("name"): f for f in _page_filters_view(page)}
    found: dict[tuple[str, str], tuple[_Column, dict[str, Any]]] = {}
    for param in params if isinstance(params, list) else []:
        if not isinstance(param, dict):
            continue
        column = _field_column(param.get("fieldExpr"))
        if column is None:
            bound = filters.get(param.get("boundFilter"))
            column = _field_column(bound.get("field")) if isinstance(bound, dict) else None
        if column is not None:
            found[_key(column)] = (column, param)
    return found


def _page_filters_view(page: PageDoc) -> list[dict[str, Any]]:
    config = page.data.get("filterConfig")
    filters = config.get("filters") if isinstance(config, dict) else None
    return [f for f in filters if isinstance(f, dict)] if isinstance(filters, list) else []


def _remove_parameter(page: PageDoc, param: Mapping[str, Any]) -> None:
    binding = page.data.get("pageBinding")
    if isinstance(binding, dict):
        binding["parameters"] = [p for p in binding.get("parameters") or [] if p is not param]
    bound = param.get("boundFilter")
    filters = _page_filters_view(page)
    # The bound filter goes with its field: Desktop creates it for the binding (howCreated
    # Drillthrough); one with no condition of its own is only the binding's too.
    kept = [
        f
        for f in filters
        if not (
            f.get("name") == bound and (f.get("howCreated") == "Drillthrough" or "filter" not in f)
        )
    ]
    if len(kept) != len(filters):
        config = page.data["filterConfig"]
        if kept:
            config["filters"] = kept
        else:
            config.pop("filters", None)
            if not config:
                page.data.pop("filterConfig", None)


def _bind_fields(
    document: PbirDocument,
    page: PageDoc,
    columns: Sequence[_Column],
    mode: str,
    binding_type: str,
) -> tuple[list[str], list[str]]:
    """Add, replace or remove the page's bound columns; returns (added, removed) as text."""
    binding = page.data.get("pageBinding")
    if not isinstance(binding, dict):
        binding = {"name": document.new_name(_binding_names(document)), "type": binding_type}
        page.data["pageBinding"] = binding
    binding["type"] = binding_type
    existing = _bound_fields(page)
    wanted = {_key(c): c for c in columns}
    added: list[str] = []
    removed: list[str] = []
    if mode == "remove":
        missing = [c.text for k, c in wanted.items() if k not in existing]
        if missing:
            raise DocumentError(f"Page {_page_label(page)} has no field {join_names(missing)}")
        for key in wanted:
            column, param = existing[key]
            _remove_parameter(page, param)
            removed.append(column.text)
        return added, removed
    if mode == "replace":
        for key, (column, param) in existing.items():
            if key not in wanted:
                _remove_parameter(page, param)
                removed.append(column.text)
    used_filters = _used_filter_names(document)
    used_params = _binding_names(document)
    for key, column in wanted.items():
        if key in existing:
            continue
        filter_name = _new_filter_name(document, used_filters)
        _page_filters(page).append(
            {
                "name": filter_name,
                "field": column.expression,
                "type": "Categorical",
                "howCreated": "Drillthrough",
            }
        )
        param_name = document.new_name(used_params)
        used_params.add(param_name)
        binding.setdefault("parameters", []).append(
            {"name": param_name, "boundFilter": filter_name, "fieldExpr": column.expression}
        )
        added.append(column.text)
    return added, removed


def _drop_empty_binding(page: PageDoc) -> bool:
    """A binding left with no fields becomes a Default one, and goes when nothing else is set
    on it (Desktop's pod cleanup); returns whether the page lost its fields."""
    binding = page.data.get("pageBinding")
    if not isinstance(binding, dict) or binding.get("parameters"):
        return False
    binding.pop("parameters", None)
    binding["type"] = "Default"
    if not (binding.get("acceptsFilterContext") or binding.get("referenceScope")):
        page.data.pop("pageBinding", None)
    return True


def _has_back_button(page: PageDoc) -> bool:
    for visual in page.visuals.values():
        for entry in _entries(visual.get("visual", {}).get("visualContainerObjects"), "visualLink"):
            props = entry.get("properties")
            if (
                isinstance(props, dict)
                and _literal(props.get("show")) is True
                and _literal(props.get("type")) == "Back"
            ):
                return True
    return False


def _add_back_button(document: PbirDocument, page: PageDoc) -> str:
    placement = choose_placement(page, *_BUTTON_SIZE, 0, 0)
    visual = new_visual(document, page, "actionButton", placement)
    _own_objects(visual)["icon"] = [
        {"properties": {"shapeType": expr.string("back")}, "selector": {"id": "default"}}
    ]
    _write_action(visual, {"show": expr.boolean(True), "type": expr.string("Back")}, None)
    attach(document, page, visual)
    return _visual_name(visual)


def _apply_set_drillthrough(
    document: PbirDocument, op: SetDrillthrough, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    if _page_type(page) == "Tooltip":
        raise DocumentError(
            f"Page {_page_label(page)} is a tooltip page; make it a normal page first, or use "
            "another page for drillthrough"
        )
    columns, warnings = _resolve_columns(model, op.fields, "a drillthrough page")
    added, removed = _bind_fields(document, page, columns, op.mode, "Drillthrough")
    page.data["type"] = "Drillthrough"
    binding = page.data["pageBinding"]
    if op.keep_all_filters is False:
        binding["acceptsFilterContext"] = "None"
    elif op.keep_all_filters is True:
        binding.pop("acceptsFilterContext", None)
    if op.cross_report is True:
        binding["referenceScope"] = "CrossReport"
    elif op.cross_report is False:
        binding.pop("referenceScope", None)
    dropped = _drop_empty_binding(page)
    if dropped:
        warnings.append(
            f"Page {_page_label(page)} has no drillthrough fields left, so nothing opens it; "
            "make it a normal page if it should not be a drillthrough page."
        )
    back = None
    if op.back_button and not dropped and not _has_back_button(page):
        back = _add_back_button(document, page)
    document.touch_page(page)
    summary = f"Page {_page_label(page)} is a drillthrough page"
    parts = []
    if added:
        parts.append(f"added {join_names(added)}")
    if removed:
        parts.append(f"removed {join_names(removed)}")
    if parts:
        summary += f" ({_all(parts)})"
    fields_now = [column.text for column, _ in _bound_fields(page).values()]
    if fields_now:
        summary += f"; right-click a data point of {join_names(fields_now)} to drill through to it"
        keep = page.data["pageBinding"].get("acceptsFilterContext") != "None"
        summary += "; it keeps all filters" if keep else "; it keeps only the drillthrough filters"
    if back:
        summary += "; a Back button was added at its top left"
    return HandlerResult(
        summary + ".", warnings=warnings, visible_in=_powerbi_only(op.op, _PAGE_VISIBLE)
    )


def _fits(page: PageDoc, width: float, height: float) -> list[str]:
    outside = []
    for name, visual in page.visuals.items():
        if is_group(visual):
            continue
        box = page_box(page, name)
        if box.right > width + 0.5 or box.bottom > height + 0.5:
            outside.append(_label(visual, page))
    return outside


def _apply_set_tooltip_page(
    document: PbirDocument, op: SetTooltipPage, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    if _page_type(page) == "Drillthrough":
        raise DocumentError(
            f"Page {_page_label(page)} is a drillthrough page; make it a normal page first, or use "
            "another page as the tooltip"
        )
    warnings: list[str] = []
    notes: list[str] = []
    if op.size != "keep":
        width, height = TOOLTIP_SIZES[op.size]
        outside = _fits(page, width, height)
        if outside:
            raise DocumentError(
                f"A {width} x {height} tooltip page cannot hold {_listed(outside)}. Move or resize "
                "them first, or keep the page's size"
            )
        page.data["width"], page.data["height"] = width, height
        notes.append(f"{width} x {height}")
    if op.hide:
        others = [p for p in document.pages.values() if p is not page]
        if not any(p.data.get("visibility") != "HiddenInViewMode" for p in others):
            raise DocumentError("Every other page is hidden, so this one has to stay visible")
        page.data["visibility"] = "HiddenInViewMode"
        notes.append("hidden from readers")
    page.data["type"] = "Tooltip"
    if op.fields:
        columns, more = _resolve_columns(model, op.fields, "a tooltip page")
        warnings.extend(more)
        added, removed = _bind_fields(document, page, columns, op.mode, "Tooltip")
        _drop_empty_binding(page)
        if added:
            notes.append(f"shown for {join_names(added)}")
        if removed:
            notes.append(f"no longer tied to {join_names(removed)}")
    document.touch_page(page)
    summary = f"Page {_page_label(page)} is a tooltip page"
    if notes:
        summary += f" ({_all(notes)})"
    summary += "; point a visual's tooltip at it to use it."
    return HandlerResult(summary, warnings=warnings, visible_in=_powerbi_only(op.op, _PAGE_VISIBLE))


def _page_links(document: PbirDocument, page: PageDoc) -> list[tuple[PageDoc, str, str, str]]:
    """(page, visual, kind, label) of drillthrough actions and tooltips that use the page."""
    found: list[tuple[PageDoc, str, str, str]] = []
    for owner, name, visual in document.all_visuals():
        body = visual.get("visual")
        if not isinstance(body, dict):
            continue
        holder = body.get("visualContainerObjects")
        who = f"{sentence(_label(visual, owner))} on page {_page_label(owner)}"
        for object_name, prop, kind, phrase in (
            ("visualLink", "drillthroughSection", "drillthrough", "drills through to it"),
            ("visualTooltip", "section", "tooltip", "uses it as its tooltip"),
        ):
            for entry in _entries(holder, object_name):
                props = entry.get("properties")
                if isinstance(props, dict) and _literal(props.get(prop)) == page.name:
                    found.append((owner, name, kind, f"{who} {phrase}"))
    return found


def _reset_tooltip(visual: dict[str, Any]) -> None:
    """Back to Power BI's default tooltip: no page, and a "show" that only says the default
    (on) goes too; its own formatting (colors, font) stays."""
    holder = _container_objects(visual)
    for entry in _entries(holder, "visualTooltip"):
        props = _props(entry)
        props.pop("section", None)
        props.pop("type", None)
        if _literal(props.get("show")) is True:
            props.pop("show")
    _drop_empty(holder, "visualTooltip")
    if not holder:
        visual["visual"].pop("visualContainerObjects", None)


def _apply_clear_page_type(
    document: PbirDocument, op: ClearPageType, model: ModelInfo
) -> HandlerResult:
    page = resolve_page(document, op.page)
    kind = _page_type(page)
    if kind is None:
        return HandlerResult(f"Page {_page_label(page)} is already a normal page.")
    links = _page_links(document, page)
    changes: list[str] = []
    if links:
        if not op.cleanup_references:
            users = _listed([link[3] for link in links])
            raise NeedsConfirmation(
                "clear_page_links",
                f"Page {_page_label(page)} is still used: {users}. Making it a normal page "
                f"switches {'that' if len(links) == 1 else 'those'} off.",
            )
        for owner, name, link, _ in links:
            visual = owner.visuals[name]
            if link == "tooltip":
                _reset_tooltip(visual)
                changes.append(f"{_label(visual, owner)} shows the default tooltip again")
            else:
                for entry in _entries(_container_objects(visual), "visualLink"):
                    props = _props(entry)
                    if _literal(props.get("drillthroughSection")) == page.name:
                        props.pop("drillthroughSection", None)
                        props.pop("type", None)
                        props["show"] = expr.boolean(False)
                changes.append(f"the action of {_label(visual, owner)} was switched off")
            document.touch_visual(owner, name)
    for _, param in list(_bound_fields(page).values()):
        _remove_parameter(page, param)
    page.data.pop("pageBinding", None)
    page.data.pop("type", None)
    document.touch_page(page)
    summary = f"Page {_page_label(page)} is a normal page again (it was a {kind.lower()} page)"
    if changes:
        summary += "; " + join_names(changes)
    summary += "."
    warnings = []
    if page.data.get("visibility") == "HiddenInViewMode":
        warnings.append(
            f"Page {_page_label(page)} is still hidden from readers; show it if they should see it."
        )
    return HandlerResult(summary, warnings=warnings, visible_in=_powerbi_only(op.op, _PAGE_VISIBLE))


_NO_TOOLTIP = frozenset(
    {
        "textbox",
        "shape",
        "basicShape",
        "image",
        "actionButton",
        "pageNavigator",
        "bookmarkNavigator",
    }
)


def _apply_set_visual_tooltip(
    document: PbirDocument, op: SetVisualTooltip, model: ModelInfo
) -> HandlerResult:
    owner, visual = _leaf(document, op.visual)
    kind = visual_kind(visual)
    if kind in _NO_TOOLTIP:
        raise DocumentError(f"{sentence(_label(visual, owner))} shows no data tooltip")
    label = _label(visual, owner)
    if op.page is None:
        _reset_tooltip(visual)
        document.touch_visual(owner, _visual_name(visual))
        return HandlerResult(
            f"{sentence(label)} shows Power BI's default tooltip again.",
            visible_in=_powerbi_only(op.op, _PAGE_VISIBLE),
        )
    page = resolve_page(document, op.page)
    if _page_type(page) != "Tooltip":
        raise DocumentError(f"Page {_page_label(page)} is not a tooltip page; make it one first")
    if page is owner:
        raise DocumentError("A visual cannot use its own page as its tooltip")
    props = _props(_general_entry(_container_objects(visual), "visualTooltip"))
    props["show"] = expr.boolean(True)
    props["type"] = expr.string("Canvas")
    props["section"] = expr.string(page.name)
    document.touch_visual(owner, _visual_name(visual))
    return HandlerResult(
        f"Hovering {label} now shows page {_page_label(page)} as its tooltip.",
        visible_in=_powerbi_only(op.op, _PAGE_VISIBLE),
    )


# --- interactions ----------------------------------------------------------------------------


def _interactions(page: PageDoc) -> list[dict[str, Any]]:
    found = page.data.get("visualInteractions")
    return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []


def _store_interactions(page: PageDoc, items: list[dict[str, Any]]) -> None:
    if items:
        page.data["visualInteractions"] = items
    else:
        page.data.pop("visualInteractions", None)


def _apply_set_interaction(
    document: PbirDocument, op: SetInteraction, model: ModelInfo
) -> HandlerResult:
    page, source = _leaf(document, op.source)
    source_name = _visual_name(source)
    if visual_kind(source) in _NOT_INTERACTIVE:
        raise DocumentError(f"{sentence(_label(source, page))} does not filter other visuals")
    if op.targets:
        targets = _names_on(document, page, op.targets, "the interaction")
        for name in targets:
            visual = page.visuals[name]
            if name == source_name:
                raise DocumentError("A visual's interaction with itself cannot be changed")
            if is_group(visual) or visual_kind(visual) in _NOT_INTERACTIVE:
                raise DocumentError(
                    f"{sentence(_label(visual, page))} is not filtered by other visuals"
                )
    else:
        targets = [
            n
            for n, v in page.visuals.items()
            if n != source_name and not is_group(v) and visual_kind(v) not in _NOT_INTERACTIVE
        ]
        if not targets:
            raise DocumentError(
                f"There is no other visual on page {_page_label(page)} to interact with"
            )
    if op.type == "highlight":
        cannot = [page.visuals[n] for n in targets if visual_kind(page.visuals[n]) in _NO_HIGHLIGHT]
        if cannot and op.targets:
            raise DocumentError(
                f"{sentence(join_names([_label(v, page) for v in cannot]))} cannot highlight; "
                "Power BI filters them instead. Choose filter or none for them"
            )
        targets = [n for n in targets if visual_kind(page.visuals[n]) not in _NO_HIGHLIGHT]
    wanted = set(targets)
    kept = [
        i
        for i in _interactions(page)
        if not (i.get("source") == source_name and i.get("target") in wanted)
    ]
    if op.type != "default":
        kept.extend(
            {"source": source_name, "target": name, "type": _INTERACTION_TYPES[op.type]}
            for name in targets
        )
    _store_interactions(page, kept)
    document.touch_page(page)
    names = [_label(page.visuals[n], page) for n in targets]
    one = len(targets) == 1
    reaction = {
        "filter": "is filtered by" if one else "are filtered by",
        "highlight": "is highlighted by" if one else "are highlighted by",
        "none": "no longer reacts to" if one else "no longer react to",
        "default": "reacts the default way to" if one else "react the default way to",
    }[op.type]
    summary = f"{sentence(join_names(names))} {reaction} selections in {_label(source, page)}."
    # Studio's canvas resolves each interaction the way render_navigation.interaction reads it:
    # the default (no entry) and every type it knows.
    if op.type == "default" or _INTERACTION_TYPES[op.type] in RENDERED_INTERACTIONS:
        visible = [VisibleIn(op.op, "studio", "Studio's canvas applies it too.")]
    else:
        visible = _powerbi_only(
            op.op,
            "Power BI applies it; Studio's canvas does once its renderer supports this kind of "
            "interaction.",
        )
    return HandlerResult(summary, visible_in=visible)


def _apply_reset_interactions(
    document: PbirDocument, op: ResetInteractions, model: ModelInfo
) -> HandlerResult:
    if (op.visual is None) == (op.page is None):
        raise DocumentError("Say either which visual's interactions to reset, or which page's")
    if op.visual is not None:
        page, visual = _leaf(document, op.visual)
        name = _visual_name(visual)
        items = _interactions(page)
        kept = [i for i in items if i.get("source") != name]
        subject = f"selections in {_label(visual, page)}"
    else:
        assert op.page is not None
        page = resolve_page(document, op.page)
        items = _interactions(page)
        kept = []
        subject = f"selections on page {_page_label(page)}"
    removed = len(items) - len(kept)
    if not removed:
        return HandlerResult(f"Every visual already reacts the default way to {subject}.")
    _store_interactions(page, kept)
    document.touch_page(page)
    return HandlerResult(
        f"Reset {_plural(removed, 'interaction setting')}: every visual reacts the default way to "
        f"{subject}.",
        visible_in=[
            VisibleIn(op.op, "studio", "Studio's canvas applies Power BI's default interactions.")
        ],
    )


# --- sync slicers ----------------------------------------------------------------------------


def _sync_group(visual: Mapping[str, Any]) -> dict[str, Any] | None:
    body = visual.get("visual")
    group = body.get("syncGroup") if isinstance(body, dict) else None
    return group if isinstance(group, dict) and isinstance(group.get("groupName"), str) else None


def _first_field(visual: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
    body = visual.get("visual")
    query = body.get("query") if isinstance(body, dict) else None
    state = query.get("queryState") if isinstance(query, dict) else None
    for role in state.values() if isinstance(state, dict) else []:
        for projection in role.get("projections") or [] if isinstance(role, dict) else []:
            if isinstance(projection, dict) and isinstance(projection.get("field"), dict):
                return projection_label(projection), projection["field"]
    return None


def _group_names(document: PbirDocument) -> set[str]:
    return {
        str(group["groupName"])
        for _, _, visual in document.all_visuals()
        if (group := _sync_group(visual)) is not None
    }


def _new_group_name(document: PbirDocument, visual: Mapping[str, Any]) -> str:
    # Desktop names a sync group after the slicer's field and adds 1, 2... to a name in use.
    field = _first_field(visual)
    base = (field[0] if field else "") or "Group"
    taken = _group_names(document)
    name, count = base, 1
    while name in taken:
        name = f"{base}{count}"
        count += 1
    return name


def _members(document: PbirDocument, group: str) -> list[tuple[PageDoc, str, dict[str, Any]]]:
    return [
        (p, n, v)
        for p, n, v in document.all_visuals()
        if (found := _sync_group(v)) is not None and found.get("groupName") == group
    ]


def _same_field(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    first, second = _first_field(a), _first_field(b)
    return first is not None and second is not None and first[1] == second[1]


def _copy_slicer(
    document: PbirDocument, source_page: PageDoc, source: dict[str, Any], target: PageDoc
) -> str:
    box = page_box(source_page, _visual_name(source))
    placement = choose_placement(target, box.width, box.height, box.x, box.y)
    copied = copy.deepcopy(source)
    copied["name"] = document.new_name(document.visual_names())
    copied.pop("parentGroupName", None)
    copied.pop("isHidden", None)
    position = copied.setdefault("position", {})
    position.update(
        {
            "x": tidy(placement.x),
            "y": tidy(placement.y),
            "z": next_z(target),
            "height": tidy(placement.height),
            "width": tidy(placement.width),
            "tabOrder": next_tab_order(target),
        }
    )
    attach(document, target, copied)
    # New filter names (unique report-wide) and the next tab stop, as a duplicated visual gets.
    remap_duplicate_visual(document, target, _visual_name(copied))
    return _visual_name(copied)


def _apply_sync_slicer(document: PbirDocument, op: SyncSlicer, model: ModelInfo) -> HandlerResult:
    source_page, source = _leaf(document, op.visual)
    kind = visual_kind(source)
    if kind not in _SYNC_TYPES:
        raise DocumentError(
            f"{sentence(_label(source, source_page))} is not a slicer; only slicers sync"
        )
    if _first_field(source) is None:
        raise DocumentError(f"{sentence(_label(source, source_page))} has no field to sync")
    if not op.pages and not op.hidden_on:
        raise DocumentError("Say which pages the slicer should sync to")
    visible_pages = {resolve_page(document, ref).name for ref in op.pages}
    hidden_pages = {resolve_page(document, ref).name for ref in op.hidden_on}
    if visible_pages & hidden_pages:
        raise DocumentError("A page cannot both show the synced slicer and hide it")
    if source_page.name in hidden_pages:
        raise DocumentError(
            "The slicer stays visible on its own page; hide it there with the visual's own setting"
        )
    wanted = [p for p in document.pages.values() if p.name in visible_pages | hidden_pages]
    wanted = [p for p in wanted if p is not source_page]
    existing = _sync_group(source)
    if op.group:
        name = op.group.strip()
        if name in _group_names(document) and (existing is None or existing["groupName"] != name):
            clash = [v for _, _, v in _members(document, name) if not _same_field(v, source)]
            if clash:
                raise DocumentError(
                    f"The sync group {name!r} holds slicers on another field; pick another name"
                )
    elif existing is not None:
        name = str(existing["groupName"])
    else:
        name = _new_group_name(document, source)
    settings = {
        "groupName": name,
        "fieldChanges": op.field_changes,
        "filterChanges": op.filter_changes,
    }
    if existing is not None and existing.get("groupName") != name:
        # Renaming the group renames it on every slicer already in it.
        for page, member, visual in _members(document, str(existing["groupName"])):
            visual["visual"]["syncGroup"] = dict(settings)
            document.touch_visual(page, member)
    source["visual"]["syncGroup"] = dict(settings)
    document.touch_visual(source_page, _visual_name(source))
    added: list[str] = []
    joined: list[str] = []
    updated: list[str] = []
    for page in wanted:
        label = _page_label(page) + (" (hidden there)" if page.name in hidden_pages else "")
        members = [v for p, _, v in _members(document, name) if p is page and v is not source]
        match = members[0] if members else None
        if match is not None:
            updated.append(label)
        else:
            match = next(
                (
                    v
                    for v in page.visuals.values()
                    if visual_kind(v) == kind and _sync_group(v) is None and _same_field(v, source)
                ),
                None,
            )
            if match is not None:
                joined.append(label)
        if match is None:
            member_name = _copy_slicer(document, source_page, source, page)
            match = page.visuals[member_name]
            added.append(label)
        match["visual"]["syncGroup"] = dict(settings)
        # Desktop's Sync slicers pane: "Visible" off keeps the synced slicer on the page, hidden.
        if page.name in hidden_pages:
            match["isHidden"] = True
        else:
            match.pop("isHidden", None)
        document.touch_visual(page, _visual_name(match))
    parts = []
    for labels, words in (
        (added, "added a synced copy to"),
        (joined, "joined the matching slicer on"),
        (updated, "kept syncing on"),
    ):
        if labels:
            parts.append(f"{words} {'pages' if len(labels) > 1 else 'page'} {join_names(labels)}")
    summary = f"{sentence(_label(source, source_page))} syncs as {name!r}"
    if parts:
        summary += "; " + _all(parts)
    summary += "."
    visible = [
        VisibleIn(
            op.op, "studio", "The copies show in Studio; the syncing itself happens in Power BI."
        )
    ]
    return HandlerResult(summary, visible_in=visible)


def _apply_unsync_slicer(
    document: PbirDocument, op: UnsyncSlicer, model: ModelInfo
) -> HandlerResult:
    source_page, source = _leaf(document, op.visual)
    group = _sync_group(source)
    if group is None:
        return HandlerResult(
            f"{sentence(_label(source, source_page))} is not synced with other pages."
        )
    name = str(group["groupName"])
    pages = {resolve_page(document, ref).name for ref in op.pages}
    leaving = [(p, n, v) for p, n, v in _members(document, name) if not pages or p.name in pages]
    deleted: list[str] = []
    left: list[str] = []
    for page, member, visual in leaving:
        if op.delete_copies and visual is not source:
            remove_visual_references(document, page, member)
            deleted.append(_page_label(page))
            continue
        visual["visual"].pop("syncGroup", None)
        document.touch_visual(page, member)
        left.append(_page_label(page))
    remaining = _members(document, name)
    if len(remaining) == 1:
        # A group of one syncs nothing; Desktop's pane shows it as not synced.
        page, member, visual = remaining[0]
        visual["visual"].pop("syncGroup", None)
        document.touch_visual(page, member)
    parts = []
    if left:
        parts.append(f"stopped syncing on {join_names(left)}")
    if deleted:
        parts.append(f"deleted the synced copies on {join_names(deleted)}")
    return HandlerResult(
        f"Sync group {name!r}: {_all(parts) or 'nothing to change'}.",
        visible_in=_powerbi_only(op.op, "Syncing happens in Power BI."),
    )


# --- drill ------------------------------------------------------------------------------------


def _apply_set_drill_filter(
    document: PbirDocument, op: SetDrillFilter, model: ModelInfo
) -> HandlerResult:
    verb = "filters" if op.filter_other_visuals else "does not filter"
    # Off is the flag left out: Desktop 2.157 drops a false it reads (probes of 2026-09-29), and
    # only ever writes true.
    if op.visual is None:
        settings = document.report.setdefault("settings", {})
        if op.filter_other_visuals:
            settings["defaultDrillFilterOtherVisuals"] = True
        else:
            settings.pop("defaultDrillFilterOtherVisuals", None)
            if not settings:
                document.report.pop("settings")
        document.touch_report()
        summary = (
            f"By default, drilling in a visual now {verb} the other visuals of its page "
            "(visuals with their own setting keep it)."
        )
    else:
        page, visual = _leaf(document, op.visual)
        if op.filter_other_visuals:
            visual["visual"]["drillFilterOtherVisuals"] = True
        else:
            visual["visual"].pop("drillFilterOtherVisuals", None)
        document.touch_visual(page, _visual_name(visual))
        summary = f"Drilling in {_label(visual, page)} now {verb} the other visuals."
    return HandlerResult(
        summary, visible_in=_powerbi_only(op.op, "Studio's canvas does not drill.")
    )


# Wells that hold values, never levels to drill through (a chart's Y, a matrix's Values...).
_VALUE_ROLES = frozenset(
    {"Values", "Y", "Y2", "Tooltips", "Size", "Gradient", "TargetValue", "Goal", "Indicator"}
)


def _is_level(projection: Mapping[str, Any]) -> bool:
    """A projection that can be a drill level: a column or a hierarchy level, not a measure or
    a summarized column."""
    field = projection.get("field")
    return isinstance(field, dict) and ("Column" in field or "HierarchyLevel" in field)


def _drill_role(visual: dict[str, Any], role: str | None) -> tuple[str, list[dict[str, Any]]]:
    state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    roles = {
        name: [p for p in value.get("projections") or [] if isinstance(p, dict)]
        for name, value in (state.items() if isinstance(state, dict) else [])
        if isinstance(value, dict)
    }
    # A well can be drilled when it holds levels only: a chart's axis, a matrix's rows.
    levels = {
        name: projections
        for name, projections in roles.items()
        if projections and name not in _VALUE_ROLES and all(_is_level(p) for p in projections)
    }
    if role is not None:
        match = next((r for r in roles if r.casefold() == role.casefold()), None)
        if match is None:
            raise DocumentError(f"This visual has no {role!r} field well")
        if match not in levels:
            raise DocumentError(
                f"The {match} well holds values (measures or summarized columns), not levels, so "
                "it cannot be drilled; drill on the axis or rows"
            )
        if len(levels[match]) < 2:
            raise DocumentError(f"The {match} well has one field, so there is no level to drill to")
        return match, levels[match]
    for name in ("Category", "Rows", "Axis", "Columns", "Series", "Legend"):
        if len(levels.get(name, [])) >= 2:
            return name, levels[name]
    deep = [(n, p) for n, p in levels.items() if len(p) >= 2]
    if not deep:
        raise DocumentError(
            "This visual has no hierarchy to drill on: put two or more fields in its axis (or "
            "rows) first"
        )
    return deep[0]


def _apply_drill_visual(document: PbirDocument, op: DrillVisual, model: ModelInfo) -> HandlerResult:
    page, visual = _leaf(document, op.visual)
    if visual_kind(visual) in _NOT_DRILLABLE | _NOT_INTERACTIVE:
        raise DocumentError(f"{sentence(_label(visual, page))} cannot be drilled")
    role, projections = _drill_role(visual, op.role)
    active = [i for i, p in enumerate(projections) if p.get("active") is True]
    if not active:
        active = [0]
    deepest = max(active)
    last = len(projections) - 1
    if op.action in {"next_level", "expand_all"} and deepest >= last:
        raise DocumentError(f"{sentence(_label(visual, page))} already shows its last level")
    if op.action == "up" and active == [0]:
        raise DocumentError(f"{sentence(_label(visual, page))} already shows its top level")
    if op.action == "next_level":
        wanted = {deepest + 1}
    elif op.action == "expand_all":
        wanted = set(range(deepest + 2))
    elif op.action == "up":
        wanted = set(active) - {deepest} if len(active) > 1 else {deepest - 1}
    else:
        wanted = {0}
    for index, projection in enumerate(projections):
        projection["active"] = index in wanted
    document.touch_visual(page, _visual_name(visual))
    shown = [projection_label(projections[i]) for i in sorted(wanted)]
    words = {
        "next_level": "now shows the next level",
        "expand_all": "is expanded down one level",
        "up": "went up one level",
        "top": "is back at its top level",
    }[op.action]
    return HandlerResult(
        f"{sentence(_label(visual, page))} {words}: {' > '.join(shown)}.",
        visible_in=_powerbi_only(
            op.op, "Studio's canvas still draws the chart's first level; Power BI shows the drill."
        ),
    )


# --- the family's table, dispatcher and parser --------------------------------------------------

NAVIGATION_HANDLERS: dict[str, Handler] = {
    "add_bookmark": _apply_add_bookmark,
    "update_bookmark": _apply_update_bookmark,
    "rename_bookmark": _apply_rename_bookmark,
    "move_bookmark": _apply_move_bookmark,
    "group_bookmarks": _apply_group_bookmarks,
    "ungroup_bookmarks": _apply_ungroup_bookmarks,
    "delete_bookmark": _apply_delete_bookmark,
    "set_bookmark_visual_hidden": _apply_set_bookmark_visual_hidden,
    "add_button": _apply_add_button,
    "set_button_action": _apply_set_button_action,
    "add_navigator": _apply_add_navigator,
    "set_navigator": _apply_set_navigator,
    "set_drillthrough": _apply_set_drillthrough,
    "set_tooltip_page": _apply_set_tooltip_page,
    "clear_page_type": _apply_clear_page_type,
    "set_visual_tooltip": _apply_set_visual_tooltip,
    "set_interaction": _apply_set_interaction,
    "reset_interactions": _apply_reset_interactions,
    "sync_slicer": _apply_sync_slicer,
    "unsync_slicer": _apply_unsync_slicer,
    "set_drill_filter": _apply_set_drill_filter,
    "drill_visual": _apply_drill_visual,
}

_ADAPTER: TypeAdapter[Any] = TypeAdapter(Annotated[NavigationOperation, Field(discriminator="op")])


def parse_navigation(raw: Sequence[Mapping[str, Any]]) -> list[NavigationOperation]:
    """Operation dicts into this family's operations (``report_edits.parse_operations`` takes
    them once the family is registered). A step that does not validate raises DocumentError
    naming it."""
    parsed: list[NavigationOperation] = []
    for index, item in enumerate(raw, start=1):
        try:
            parsed.append(_ADAPTER.validate_python(item))
        except ValidationError as error:
            first = error.errors(include_url=False, include_input=False)[0]
            where = ".".join(str(p) for p in first["loc"][1:]) or "the step"
            raise DocumentError(
                f"Step {index} ({item.get('op')}): {where}: {first['msg']}"
            ) from error
    return parsed


def apply_navigation(
    document: PbirDocument, operations: Sequence[Any], model: ModelInfo
) -> EditOutcome:
    """Apply navigation operations in order, all or none, the way
    ``report_edits.apply_operations`` applies a batch (the family is registered there by the
    integration step; this is what its tests call until then)."""
    outcome = EditOutcome()
    for index, operation in enumerate(operations):
        handler = NAVIGATION_HANDLERS[operation.op]
        try:
            result = handler(document, operation, model)
        except expr.ExpressionError as error:
            raise DocumentError(str(error)) from error
        except (NeedsConfirmation, NeedsChoice) as error:
            error.step = index + 1
            raise
        except DocumentError as error:
            prefix = f"Step {index + 1}: " if len(operations) > 1 else ""
            raise DocumentError(prefix + str(error)) from error
        if isinstance(result, HandlerResult):
            outcome.summaries.append(result.summary)
            outcome.warnings.extend(result.warnings)
            outcome.protected.extend(result.protected)
            outcome.replaced.extend(result.replaced)
            outcome.visible_in.extend(result.visible_in)
            outcome.model_operations.extend(result.model_operations)
            outcome.pending_model_operations.extend(result.pending_model_operations)
            outcome.lineage.update(result.lineage)
            outcome.partial = outcome.partial or result.partial
        else:
            outcome.summaries.append(result)
        outcome.report_changed = True
    return outcome


# --- consistency ------------------------------------------------------------------------------


def _finding_label(page: PageDoc, name: str, visual: dict[str, Any]) -> str:
    return _label(visual, page)


def navigation_findings(document: PbirDocument) -> list[NavigationFinding]:
    """Every dangling or inconsistent navigation reference ([] when clean), as
    ``report_refs.navigation_findings`` finds them (the structural validator blocks a save on
    the same list), with visuals named the way this family names them."""
    return refs_navigation_findings(document, _finding_label)


def navigation_problems(document: PbirDocument) -> list[str]:
    """``navigation_findings`` in plain English ([] when clean)."""
    return [finding.message for finding in navigation_findings(document)]


# --- tool cards ---------------------------------------------------------------------------------
# Ids of the Agents dashboard (its structure twin fixtures/imported/structure-twin.pbix: one
# page named "1"). ``EXAMPLE_SETUP`` is the batch every example assumes has run first.

PAGE = "1"
TABLE = "e82e02f93055d6b0b544"  # table of agents
CHART_TOTAL = "b404a461ede7604a2774"  # column chart 'Total MTD Sales'
CHART_AVG = "da32cdbb0090e885649b"  # column chart 'Avg MTD Sales by Agents'
CHART_CHANGE = "ab13db00c71dcec86e99"
CHART_DISCOUNT = "97d1504080b699eae7d7"
CARD_POSITIVE = "fb4c5aa34e0b084b62b9"
CARD_AVG = "2202e311b1493069e4eb"
CARD_CHANGE = "9479353528b0eb22b958"
CARD_100K = "71a164765d9c63200302"
SLICER_TOPN = "953a59a3cad98b0b5a39"
SLICER_STORE = "5b0dbccd61c6c4adb415"
SLICER_COUNTRY = "54be5cf0783e66471540"
SLICER_CHANNEL = "b85aa81a8b5c60030d13"
CARDS = (CARD_POSITIVE, CARD_AVG, CARD_CHANGE, CARD_100K)
CHARTS = (CHART_TOTAL, CHART_AVG, CHART_CHANGE, CHART_DISCOUNT)

EXAMPLE_SETUP: tuple[Mapping[str, Any], ...] = (
    {"op": "add_page", "name": "Agent details"},
    {"op": "add_page", "name": "Agent tooltip"},
    {"op": "add_page", "name": "Stores"},
    {"op": "add_page", "name": "Spare"},
    {
        "op": "set_drillthrough",
        "page": "Agent details",
        "fields": [{"table": "DimEmployee", "name": "EmployeeKey"}],
    },
    {"op": "set_tooltip_page", "page": "Agent tooltip"},
    {"op": "add_bookmark", "name": "Overview", "page": PAGE},
    {"op": "add_bookmark", "name": "Top agents", "page": PAGE, "hide": list(CARDS)},
    {"op": "group_bookmarks", "bookmarks": ["Overview", "Top agents"], "name": "Views"},
    {
        "op": "add_button",
        "page": PAGE,
        "kind": "blank",
        "text": "Details",
        "action": {"type": "page", "page": "Stores"},
        "x": 20,
        "y": 20,
    },
    {"op": "add_navigator", "page": PAGE, "kind": "bookmark", "x": 600, "y": 40},
    {
        "op": "set_fields",
        "visual": CHART_TOTAL,
        "role": "Category",
        "mode": "add",
        "fields": [{"table": "DimChannel", "name": "ChannelName"}],
    },
    {"op": "sync_slicer", "visual": SLICER_CHANNEL, "pages": ["Stores"]},
    {"op": "set_interaction", "source": SLICER_COUNTRY, "targets": [CHART_TOTAL], "type": "none"},
    {"op": "set_interaction", "source": CHART_TOTAL, "targets": [CHART_AVG], "type": "filter"},
)

_BOOKMARK_SLOT = Slot("bookmark", "text", "the bookmark, by its name")
_PAGE_SLOT = Slot("page", "page", "the page")
_VISUAL_SLOT = Slot("visual", "visual", "the visual")
_ACTION_SLOTS = (
    Slot(
        "action",
        "enum",
        "what clicking does",
        False,
        "action.type",
        ("back", "bookmark", "page", "drillthrough", "web_url", "qna", "none"),
    ),
    Slot("target_bookmark", "text", "the bookmark it opens", False, "action.bookmark"),
    Slot("target_page", "page", "the page it goes or drills through to", False, "action.page"),
    Slot("url", "text", "the web address it opens", False, "action.url"),
)
_CHART_LIKE = (
    "columnChart",
    "clusteredColumnChart",
    "barChart",
    "clusteredBarChart",
    "lineChart",
    "areaChart",
    "stackedAreaChart",
    "pieChart",
    "donutChart",
    "pivotTable",
)


def _names(value: Any) -> list[str]:
    """A list of names from a slot: a list as it is, or text split on commas and "and"."""
    if isinstance(value, list | tuple):
        items = [str(v) for v in value]
    else:
        items = re.split(r"\s*(?:,|;|\band\b)\s*", str(value or ""))
    return [item.strip().strip("'\"").strip() for item in items if item.strip().strip("'\"")]


def _with_lists(op: str, lists: tuple[str, ...]) -> Callable[[Mapping[str, Any]], dict[str, Any]]:
    """A card builder for slots that map onto fields of the same name, where the ``lists``
    slots (a text slot the planner fills with "A and B") become lists of names."""

    def build(filled: Mapping[str, Any]) -> dict[str, Any]:
        operation: dict[str, Any] = {"op": op}
        for key, value in filled.items():
            if value is not None:
                operation[key] = _names(value) if key in lists else value
        return operation

    return build


def _card(
    op: str,
    capability: str,
    summary: str,
    slots: tuple[Slot, ...],
    cues: tuple[str, ...],
    examples: tuple[tuple[str, Mapping[str, Any]], ...],
    *,
    confirm: bool = False,
    visual_types: tuple[str, ...] = (),
    lists: tuple[str, ...] = (),
) -> ToolCard:
    return ToolCard(
        op=op,
        family=FAMILY,
        summary=summary,
        slots=slots,
        cues=cues,
        examples=examples,
        confirm=confirm,
        capability=capability,
        visual_types=visual_types,
        build=_with_lists(op, lists) if lists else None,
    )


TOOL_CARDS: tuple[ToolCard, ...] = (
    _card(
        "add_bookmark",
        "bookmarks",
        "Add a bookmark of a page as it is now: which visuals are hidden, optionally its filters, "
        "only some visuals, or visuals hidden in the bookmark only.",
        (
            Slot("name", "text", "the bookmark's name"),
            Slot("page", "page", "the page it captures (the page on screen when not said)", False),
            Slot("data", "bool", "true to remember filters, slicers and sorts too", False),
            Slot("display", "bool", "false to not remember which visuals are shown", False),
            Slot("current_page", "bool", "false so opening it stays on the current page", False),
            Slot("visuals", "visuals", "only these visuals (Selected visuals)", False),
            Slot("hide", "visuals", "visuals hidden in this bookmark only", False),
            Slot("group", "text", "an existing bookmark group to put it in", False),
        ),
        (
            "bookmark",
            "add a bookmark",
            "save this view",
            "capture",
            "snapshot of the page",
            "reset the slicers",
        ),
        (
            (
                "add a bookmark called Overview page",
                {"op": "add_bookmark", "name": "Overview page", "page": PAGE},
            ),
            (
                # The slicers as they are now (cleared): opening it resets them. Data on, and
                # display off so it does not also show or hide visuals.
                "add a bookmark 'Reset' that resets the slicers",
                {
                    "op": "add_bookmark",
                    "name": "Reset",
                    "page": PAGE,
                    "data": True,
                    "display": False,
                },
            ),
            (
                "bookmark page 1 with the cards hidden as 'Charts only'",
                {"op": "add_bookmark", "name": "Charts only", "page": PAGE, "hide": list(CARDS)},
            ),
            (
                "save the slicer selections as a bookmark named Filters",
                {
                    "op": "add_bookmark",
                    "name": "Filters",
                    "page": PAGE,
                    "data": True,
                    "display": False,
                    "visuals": [SLICER_STORE, SLICER_COUNTRY],
                },
            ),
            (
                "add a bookmark 'Table view' with the four charts hidden to the Views group",
                {
                    "op": "add_bookmark",
                    "name": "Table view",
                    "page": PAGE,
                    "hide": list(CHARTS),
                    "group": "Views",
                },
            ),
        ),
    ),
    _card(
        "update_bookmark",
        "bookmarks",
        "Update a bookmark to the page as it is now, or change what it remembers (data, display, "
        "current page, selected visuals).",
        (
            _BOOKMARK_SLOT,
            Slot(
                "recapture",
                "bool",
                "false to keep the remembered state and only change options",
                False,
            ),
            Slot("data", "bool", "remember filters, slicers and sorts", False),
            Slot("display", "bool", "remember which visuals are shown", False),
            Slot("current_page", "bool", "opening it goes to its page", False),
        ),
        ("update bookmark", "refresh the bookmark", "bookmark should remember", "bookmark options"),
        (
            ("update the Overview bookmark", {"op": "update_bookmark", "bookmark": "Overview"}),
            (
                "make the Top agents bookmark keep the filters too",
                {"op": "update_bookmark", "bookmark": "Top agents", "data": True},
            ),
            (
                "the Overview bookmark should not change page",
                {
                    "op": "update_bookmark",
                    "bookmark": "Overview",
                    "recapture": False,
                    "current_page": False,
                },
            ),
        ),
    ),
    _card(
        "rename_bookmark",
        "bookmarks",
        "Rename a bookmark (buttons that open it keep working).",
        (_BOOKMARK_SLOT, Slot("name", "text", "the new name")),
        ("rename bookmark", "call the bookmark"),
        (
            (
                "rename the Overview bookmark to Summary",
                {"op": "rename_bookmark", "bookmark": "Overview", "name": "Summary"},
            ),
            (
                "call the Top agents bookmark Best agents",
                {"op": "rename_bookmark", "bookmark": "Top agents", "name": "Best agents"},
            ),
            (
                "rename bookmark 'Top agents' to 'Leaders'",
                {"op": "rename_bookmark", "bookmark": "Top agents", "name": "Leaders"},
            ),
        ),
    ),
    _card(
        "move_bookmark",
        "bookmarks",
        "Reorder bookmarks in the Bookmarks pane, or move one into or out of a group.",
        (
            _BOOKMARK_SLOT,
            Slot("position", "integer", "its new position, 1 is first", False, None, (), 1, 1000),
            Slot("group", "text", "a group to move it into", False),
            Slot("top_level", "bool", "true to move it out of its group", False),
        ),
        ("move bookmark", "reorder bookmarks", "bookmark first", "out of the group"),
        (
            (
                "put the Top agents bookmark first",
                {"op": "move_bookmark", "bookmark": "Top agents", "position": 1},
            ),
            (
                "take Overview out of its group",
                {"op": "move_bookmark", "bookmark": "Overview", "top_level": True},
            ),
            (
                "move the Overview bookmark to second place",
                {"op": "move_bookmark", "bookmark": "Overview", "position": 2},
            ),
        ),
    ),
    _card(
        "group_bookmarks",
        "bookmarks",
        "Group bookmarks under a name in the Bookmarks pane (a bookmark navigator can then list "
        "only that group).",
        (
            Slot("bookmarks", "text", "the bookmarks to group, by name"),
            Slot("name", "text", "the group's name"),
        ),
        ("group bookmarks", "bookmark group", "put the bookmarks together"),
        (
            (
                "group Overview and Top agents as Pages",
                {"op": "group_bookmarks", "bookmarks": ["Overview", "Top agents"], "name": "Pages"},
            ),
            (
                "put the Overview bookmark in a group called Main",
                {"op": "group_bookmarks", "bookmarks": ["Overview"], "name": "Main"},
            ),
            (
                "group the Top agents bookmark as Rankings",
                {"op": "group_bookmarks", "bookmarks": ["Top agents"], "name": "Rankings"},
            ),
        ),
        lists=("bookmarks",),
    ),
    _card(
        "ungroup_bookmarks",
        "bookmarks",
        "Ungroup a bookmark group; its bookmarks stay where the group was.",
        (Slot("group", "text", "the group, by name"),),
        ("ungroup bookmarks", "remove the bookmark group"),
        (
            ("ungroup the Views bookmarks", {"op": "ungroup_bookmarks", "group": "Views"}),
            ("remove the bookmark group Views", {"op": "ungroup_bookmarks", "group": "Views"}),
            ("split the Views group", {"op": "ungroup_bookmarks", "group": "views"}),
        ),
    ),
    _card(
        "delete_bookmark",
        "bookmarks",
        "Delete a bookmark; buttons or navigators that open it are named and switched off only "
        "after a yes.",
        (
            _BOOKMARK_SLOT,
            Slot("cleanup_references", "bool", "the yes: switch off what uses it", False),
        ),
        ("delete bookmark", "remove the bookmark"),
        (
            ("delete the Overview bookmark", {"op": "delete_bookmark", "bookmark": "Overview"}),
            ("remove bookmark Top agents", {"op": "delete_bookmark", "bookmark": "Top agents"}),
            (
                "yes, delete the Overview bookmark and switch off its buttons",
                {"op": "delete_bookmark", "bookmark": "Overview", "cleanup_references": True},
            ),
        ),
        confirm=True,
    ),
    _card(
        "set_bookmark_visual_hidden",
        "bookmarks",
        "Hide or show a visual in one bookmark only (the selection pane of that bookmark); the "
        "page itself does not change.",
        (
            _BOOKMARK_SLOT,
            _VISUAL_SLOT,
            Slot("hidden", "bool", "true hides it, false shows it", False),
        ),
        ("hide in the bookmark", "hidden in bookmark", "show in the bookmark", "selection pane"),
        (
            (
                "hide the table in the Overview bookmark",
                {"op": "set_bookmark_visual_hidden", "bookmark": "Overview", "visual": TABLE},
            ),
            (
                "show the Agents Avg MTD Sales card in Top agents",
                {
                    "op": "set_bookmark_visual_hidden",
                    "bookmark": "Top agents",
                    "visual": CARD_AVG,
                    "hidden": False,
                },
            ),
            (
                "in the Overview bookmark hide the store slicer",
                {
                    "op": "set_bookmark_visual_hidden",
                    "bookmark": "Overview",
                    "visual": SLICER_STORE,
                    "hidden": True,
                },
            ),
        ),
    ),
    _card(
        "add_button",
        "buttons_actions",
        "Add a button (blank, back, bookmark, information, help, Q&A, left or right arrow, reset, "
        "Apply all slicers, Clear all slicers) with text, a tooltip and what clicking it does.",
        (
            _PAGE_SLOT,
            Slot("kind", "enum", "the button style", False, None, tuple(_BUTTON_ICONS)),
            Slot("text", "text", "the words on the button", False),
            *_ACTION_SLOTS,
            Slot("tooltip", "text", "the tooltip it shows", False),
        ),
        (
            "add a button",
            "insert button",
            "back button",
            "navigation button",
            "button that goes to",
            "apply all slicers",
            "clear all slicers",
        ),
        (
            ("add a back button", {"op": "add_button", "page": PAGE, "kind": "back"}),
            (
                "add a Clear all slicers button",
                {"op": "add_button", "page": PAGE, "kind": "clear_all_slicers"},
            ),
            (
                "add a button 'Details' that goes to page Stores",
                {
                    "op": "add_button",
                    "page": PAGE,
                    "kind": "blank",
                    "text": "Details",
                    "action": {"type": "page", "page": "Stores"},
                },
            ),
            (
                "add a bookmark button that opens Overview",
                {
                    "op": "add_button",
                    "page": PAGE,
                    "kind": "bookmark",
                    "action": {"type": "bookmark", "bookmark": "Overview"},
                },
            ),
            (
                "add an information button that opens https://contoso.com/help with the tooltip "
                "'How to read this report'",
                {
                    "op": "add_button",
                    "page": PAGE,
                    "kind": "information",
                    "action": {"type": "web_url", "url": "https://contoso.com/help"},
                    "tooltip": "How to read this report",
                },
            ),
        ),
    ),
    _card(
        "set_button_action",
        "buttons_actions",
        "Set what a button, shape or picture does when clicked (back, bookmark, go to a page, "
        "drill through, web link, Q&A, nothing) and its tooltip.",
        (_VISUAL_SLOT, *_ACTION_SLOTS, Slot("tooltip", "text", "the tooltip text", False)),
        (
            "when clicked",
            "on click",
            "link the button",
            "button goes to",
            "button opens",
            "drill through button",
        ),
        (
            (
                "make the Details button go to page Stores",
                {
                    "op": "set_button_action",
                    "visual": "Details",
                    "action": {"type": "page", "page": "Stores"},
                },
            ),
            (
                "the Details button should drill through to Agent details",
                {
                    "op": "set_button_action",
                    "visual": "Details",
                    "action": {"type": "drillthrough", "page": "Agent details"},
                },
            ),
            (
                "give the Details button the tooltip 'Open the agent page'",
                {"op": "set_button_action", "visual": "Details", "tooltip": "Open the agent page"},
            ),
        ),
        visual_types=tuple(sorted(_LINKABLE)),
    ),
    _card(
        "add_navigator",
        "buttons_actions",
        "Add a page navigator (a button per page) or a bookmark navigator (a button per "
        "bookmark, or per bookmark of a group).",
        (
            _PAGE_SLOT,
            Slot("kind", "enum", "page or bookmark navigator", False, None, ("page", "bookmark")),
            Slot("orientation", "enum", "layout", False, None, ("horizontal", "vertical", "grid")),
            Slot("bookmark_group", "text", "the bookmark group it lists", False),
        ),
        ("page navigator", "bookmark navigator", "navigation bar", "menu of pages", "tabs"),
        (
            ("add a page navigator", {"op": "add_navigator", "page": PAGE, "kind": "page"}),
            (
                "add a vertical page navigator",
                {"op": "add_navigator", "page": PAGE, "kind": "page", "orientation": "vertical"},
            ),
            (
                "add a bookmark navigator for the Views group",
                {
                    "op": "add_navigator",
                    "page": PAGE,
                    "kind": "bookmark",
                    "bookmark_group": "Views",
                },
            ),
        ),
    ),
    _card(
        "set_navigator",
        "buttons_actions",
        "Change a navigator: layout (horizontal, vertical, grid of rows and columns), which "
        "bookmark group, which pages it lists.",
        (
            _VISUAL_SLOT,
            Slot("orientation", "enum", "layout", False, None, ("horizontal", "vertical", "grid")),
            Slot("rows", "integer", "grid rows", False, None, (), 1, 10),
            Slot("columns", "integer", "grid columns", False, None, (), 1, 10),
            Slot("bookmark_group", "text", "the bookmark group it lists", False),
            Slot("hide_pages", "text", "pages to leave out", False),
            Slot(
                "deselection_bookmark",
                "text",
                "the bookmark it opens when the selected button is clicked again",
                False,
            ),
        ),
        (
            "navigator layout",
            "navigator grid",
            "leave out of the navigator",
            "navigator pages",
            "on deselection",
        ),
        (
            (
                "show the bookmark navigator as a 2 by 2 grid",
                {"op": "set_navigator", "visual": "bookmark navigator", "rows": 2, "columns": 2},
            ),
            (
                "make the bookmark navigator vertical",
                {"op": "set_navigator", "visual": "bookmark navigator", "orientation": "vertical"},
            ),
            (
                "the bookmark navigator should list the Views group",
                {"op": "set_navigator", "visual": "bookmark navigator", "bookmark_group": "Views"},
            ),
            (
                "clicking the selected bookmark again should open Overview",
                {
                    "op": "set_navigator",
                    "visual": "bookmark navigator",
                    "deselection_bookmark": "Overview",
                },
            ),
        ),
        visual_types=("bookmarkNavigator", "pageNavigator"),
        lists=("hide_pages",),
    ),
    _card(
        "set_drillthrough",
        "drillthrough_tooltip_pages",
        "Make a page a drillthrough page for columns, or add, replace or remove its drillthrough "
        "fields; keep all filters or not; a Back button is added.",
        (
            _PAGE_SLOT,
            Slot("fields", "fields", "the columns people drill through on"),
            Slot(
                "mode",
                "enum",
                "replace, add or remove fields",
                False,
                None,
                ("replace", "add", "remove"),
            ),
            Slot("keep_all_filters", "bool", "pass every filter along (on by default)", False),
        ),
        ("drillthrough", "drill through", "drill-through page", "detail page for"),
        (
            (
                "make Agent details a drillthrough page on the agent",
                {
                    "op": "set_drillthrough",
                    "page": "Agent details",
                    "fields": [{"table": "DimEmployee", "name": "EmployeeKey"}],
                },
            ),
            (
                "add store to the drillthrough of Agent details",
                {
                    "op": "set_drillthrough",
                    "page": "Agent details",
                    "fields": [{"table": "DimStore", "name": "StoreName"}],
                    "mode": "add",
                },
            ),
            (
                "make Stores a drillthrough page for StoreName that keeps only the store filter",
                {
                    "op": "set_drillthrough",
                    "page": "Stores",
                    "fields": [{"table": "DimStore", "name": "StoreName"}],
                    "keep_all_filters": False,
                },
            ),
        ),
    ),
    _card(
        "set_tooltip_page",
        "drillthrough_tooltip_pages",
        "Make a page a report-page tooltip: tooltip size, hidden from readers, optionally shown "
        "only for some fields.",
        (
            _PAGE_SLOT,
            Slot(
                "size",
                "enum",
                "tooltip size",
                False,
                None,
                ("single_visual", "multiple_visuals", "mini_report", "keep"),
            ),
            Slot("hide", "bool", "false keeps the page visible to readers", False),
            Slot("fields", "fields", "only show it for these fields", False),
        ),
        ("tooltip page", "report page tooltip", "custom tooltip", "use as tooltip"),
        (
            ("make Spare a tooltip page", {"op": "set_tooltip_page", "page": "Spare"}),
            (
                "make Spare a larger tooltip page",
                {"op": "set_tooltip_page", "page": "Spare", "size": "multiple_visuals"},
            ),
            (
                "make Spare a tooltip for the store field",
                {
                    "op": "set_tooltip_page",
                    "page": "Spare",
                    "fields": [{"table": "DimStore", "name": "StoreName"}],
                },
            ),
        ),
    ),
    _card(
        "clear_page_type",
        "drillthrough_tooltip_pages",
        "Turn a drillthrough or tooltip page back into a normal page (buttons and visuals that "
        "use it are named and switched off only after a yes).",
        (_PAGE_SLOT, Slot("cleanup_references", "bool", "the yes: switch off what uses it", False)),
        ("normal page", "no longer a drillthrough", "not a tooltip page", "remove drillthrough"),
        (
            (
                "Agent details should be a normal page",
                {"op": "clear_page_type", "page": "Agent details"},
            ),
            (
                "stop using Agent tooltip as a tooltip",
                {"op": "clear_page_type", "page": "Agent tooltip"},
            ),
            (
                "yes, make Agent details a normal page and switch off its buttons",
                {"op": "clear_page_type", "page": "Agent details", "cleanup_references": True},
            ),
        ),
        confirm=True,
    ),
    _card(
        "set_visual_tooltip",
        "drillthrough_tooltip_pages",
        "Show a tooltip page when hovering a visual, or go back to the default tooltip.",
        (_VISUAL_SLOT, Slot("page", "page", "the tooltip page (none: default tooltip)", False)),
        ("tooltip", "on hover show", "hover page"),
        (
            (
                "use Agent tooltip as the tooltip of Total MTD Sales",
                {"op": "set_visual_tooltip", "visual": CHART_TOTAL, "page": "Agent tooltip"},
            ),
            (
                "show the Agent tooltip page when hovering the table",
                {"op": "set_visual_tooltip", "visual": TABLE, "page": "Agent tooltip"},
            ),
            (
                "give Avg MTD Sales by Agents its default tooltip back",
                {"op": "set_visual_tooltip", "visual": CHART_AVG},
            ),
        ),
    ),
    _card(
        "set_interaction",
        "edit_interactions",
        "Edit interactions: how other visuals react to a selection in one visual (filter, "
        "highlight, none, default).",
        (
            Slot("source", "visual", "the visual people click in"),
            Slot("targets", "visuals", "the visuals that react (all others when not said)", False),
            Slot(
                "type",
                "enum",
                "how they react",
                True,
                None,
                ("filter", "highlight", "none", "default"),
            ),
        ),
        (
            "interaction",
            "cross-filter",
            "cross-highlight",
            "stop filtering",
            "should not filter",
            "edit interactions",
        ),
        (
            (
                "the Country slicer should not filter Total MTD Sales",
                {
                    "op": "set_interaction",
                    "source": SLICER_COUNTRY,
                    "targets": [CHART_TOTAL],
                    "type": "none",
                },
            ),
            (
                "clicking Total MTD Sales should filter Avg MTD Sales by Agents, not highlight",
                {
                    "op": "set_interaction",
                    "source": CHART_TOTAL,
                    "targets": [CHART_AVG],
                    "type": "filter",
                },
            ),
            (
                "Total MTD Sales should not affect anything",
                {"op": "set_interaction", "source": CHART_TOTAL, "type": "none"},
            ),
        ),
    ),
    _card(
        "reset_interactions",
        "edit_interactions",
        "Reset interactions to Power BI's defaults for one visual or a whole page.",
        (
            Slot("visual", "visual", "the source visual", False),
            Slot("page", "page", "or the whole page", False),
        ),
        ("reset interactions", "default interactions"),
        (
            (
                "reset the interactions of the Country slicer",
                {"op": "reset_interactions", "visual": SLICER_COUNTRY},
            ),
            ("reset all interactions on page 1", {"op": "reset_interactions", "page": PAGE}),
            (
                "put Total MTD Sales back to default interactions",
                {"op": "reset_interactions", "visual": CHART_TOTAL},
            ),
        ),
    ),
    _card(
        "sync_slicer",
        "sync_slicers",
        "Sync a slicer to other pages (a synced copy on each, or hidden but synced), naming the "
        "sync group.",
        (
            Slot("visual", "visual", "the slicer"),
            Slot("pages", "text", "pages where it syncs and shows", False),
            Slot("hidden_on", "text", "pages where it syncs but is not visible", False),
            Slot("group", "text", "the sync group's name", False),
        ),
        ("sync slicer", "sync slicers", "slicer on every page", "same slicer on", "synced"),
        (
            (
                "sync the store slicer to page Stores",
                {"op": "sync_slicer", "visual": SLICER_STORE, "pages": ["Stores"]},
            ),
            (
                "sync the Country slicer to Stores but keep it hidden there",
                {"op": "sync_slicer", "visual": SLICER_COUNTRY, "hidden_on": ["Stores"]},
            ),
            (
                "sync the store slicer to Stores and Agent details as 'Store filter'",
                {
                    "op": "sync_slicer",
                    "visual": SLICER_STORE,
                    "pages": ["Stores", "Agent details"],
                    "group": "Store filter",
                },
            ),
        ),
        visual_types=tuple(sorted(_SYNC_TYPES)),
        lists=("pages", "hidden_on"),
    ),
    _card(
        "unsync_slicer",
        "sync_slicers",
        "Stop syncing a slicer on some pages or everywhere, optionally deleting the copies.",
        (
            Slot("visual", "visual", "the slicer"),
            Slot("pages", "text", "pages where it stops syncing (all when not said)", False),
            Slot("delete_copies", "bool", "true also deletes the copies", False),
        ),
        ("unsync", "stop syncing", "remove the synced slicer"),
        (
            ("stop syncing the channel slicer", {"op": "unsync_slicer", "visual": SLICER_CHANNEL}),
            (
                "stop syncing the channel slicer on Stores",
                {"op": "unsync_slicer", "visual": SLICER_CHANNEL, "pages": ["Stores"]},
            ),
            (
                "remove the synced copies of the channel slicer",
                {"op": "unsync_slicer", "visual": SLICER_CHANNEL, "delete_copies": True},
            ),
        ),
        visual_types=tuple(sorted(_SYNC_TYPES)),
        lists=("pages",),
    ),
    _card(
        "set_drill_filter",
        "drillthrough_tooltip_pages",
        "Whether drilling in a visual filters the other visuals, for one visual or as the "
        "report's default.",
        (
            Slot("visual", "visual", "the visual (the report default when not said)", False),
            Slot("filter_other_visuals", "bool", "true filters the others"),
        ),
        ("drilling filters other visuals", "drill filter", "drill should not filter"),
        (
            (
                "drilling in Total MTD Sales should not filter other visuals",
                {"op": "set_drill_filter", "visual": CHART_TOTAL, "filter_other_visuals": False},
            ),
            (
                "drilling should filter other visuals by default",
                {"op": "set_drill_filter", "filter_other_visuals": True},
            ),
            (
                "make drill in Avg MTD Sales by Agents filter the page",
                {"op": "set_drill_filter", "visual": CHART_AVG, "filter_other_visuals": True},
            ),
        ),
    ),
    _card(
        "drill_visual",
        "drillthrough_tooltip_pages",
        "Drill a chart on its hierarchy: next level, expand all down one level, up one level, "
        "back to the top.",
        (
            _VISUAL_SLOT,
            Slot(
                "action",
                "enum",
                "which drill",
                True,
                None,
                ("next_level", "expand_all", "up", "top"),
            ),
        ),
        ("drill down", "next level", "expand all", "drill up", "hierarchy"),
        (
            (
                "drill Total MTD Sales down to the next level",
                {"op": "drill_visual", "visual": CHART_TOTAL, "action": "next_level"},
            ),
            (
                "expand Total MTD Sales all down one level",
                {"op": "drill_visual", "visual": CHART_TOTAL, "action": "expand_all"},
            ),
            (
                "show Total MTD Sales at its top level",
                {"op": "drill_visual", "visual": CHART_TOTAL, "action": "top"},
            ),
        ),
        visual_types=_CHART_LIKE,
    ),
)


# --- Desktop probes -------------------------------------------------------------------------------


def _parts_with(parts: Mapping[str, Any], suffix: str) -> list[dict[str, Any]]:
    return [v for k, v in parts.items() if k.endswith(suffix) and isinstance(v, dict)]


def _bookmark_named(parts: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (b for b in _parts_with(parts, ".bookmark.json") if b.get("displayName") == name), None
    )


def _page_named(parts: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    return next((p for p in _parts_with(parts, "/page.json") if p.get("displayName") == name), None)


def _visuals(parts: Mapping[str, Any]) -> list[dict[str, Any]]:
    return _parts_with(parts, "/visual.json")


def _check_bookmark(name: str, hidden: int) -> Callable[[Mapping[str, Any]], list[str]]:
    def check(parts: Mapping[str, Any]) -> list[str]:
        bookmark = _bookmark_named(parts, name)
        if bookmark is None:
            return [f"Desktop dropped the bookmark {name!r}"]
        state = bookmark.get("explorationState", {})
        count = sum(
            1
            for section in state.get("sections", {}).values()
            for vc in section.get("visualContainers", {}).values()
            if vc.get("singleVisual", {}).get("display", {}).get("mode") == "hidden"
        )
        return [] if count == hidden else [f"{name!r} hides {count} visuals, expected {hidden}"]

    return check


def _check_page_type(name: str, kind: str | None) -> Callable[[Mapping[str, Any]], list[str]]:
    """The page kept its type (None: a normal page again, with no binding left)."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        page = _page_named(parts, name)
        if page is None:
            return [f"Desktop dropped the page {name!r}"]
        problems = []
        if page.get("type") != kind:
            problems.append(f"page {name!r} has type {page.get('type')!r}, expected {kind!r}")
        binding = page.get("pageBinding")
        has_fields = isinstance(binding, dict) and bool(binding.get("parameters"))
        if kind is not None and not has_fields:
            problems.append(f"page {name!r} lost its fields")
        if kind is None and binding is not None:
            problems.append(f"page {name!r} still has a drillthrough or tooltip binding")
        return problems

    return check


def _check_visual_property(
    object_path: tuple[str, ...], expected: str
) -> Callable[[Mapping[str, Any]], list[str]]:
    def check(parts: Mapping[str, Any]) -> list[str]:
        for visual in _visuals(parts):
            node: Any = visual
            for key in object_path:
                node = node.get(key) if isinstance(node, dict) else None
            if node == expected or (isinstance(node, list) and expected in str(node)):
                return []
        return [f"no visual kept {'.'.join(object_path)} = {expected!r}"]

    return check


def _check_interactions(
    expected: Mapping[tuple[str, str], str],
) -> Callable[[Mapping[str, Any]], list[str]]:
    def check(parts: Mapping[str, Any]) -> list[str]:
        found: dict[tuple[str, str], Any] = {
            (str(i.get("source")), str(i.get("target"))): i.get("type")
            for page in _parts_with(parts, "/page.json")
            for i in page.get("visualInteractions", []) or []
            if isinstance(i, dict)
        }
        return [
            f"the interaction {source} > {target} is {found.get((source, target))!r}, "
            f"expected {value!r}"
            for (source, target), value in expected.items()
            if found.get((source, target)) != value
        ]

    return check


def _check_sync(group: str, count: int) -> Callable[[Mapping[str, Any]], list[str]]:
    def check(parts: Mapping[str, Any]) -> list[str]:
        members = [
            v
            for v in _visuals(parts)
            if v.get("visual", {}).get("syncGroup", {}).get("groupName") == group
        ]
        return (
            []
            if len(members) == count
            else [f"sync group {group!r} has {len(members)} slicers, expected {count}"]
        )

    return check


def _check_active(visual: str, expected: list[bool]) -> Callable[[Mapping[str, Any]], list[str]]:
    def check(parts: Mapping[str, Any]) -> list[str]:
        for data in _visuals(parts):
            if data.get("name") != visual:
                continue
            projections = (
                data.get("visual", {})
                .get("query", {})
                .get("queryState", {})
                .get("Category", {})
                .get("projections", [])
            )
            flags = [p.get("active") is True for p in projections]
            return [] if flags == expected else [f"drill flags are {flags}, expected {expected}"]
        return [f"the visual {visual} is gone"]

    return check


def _objects_of(visual: Mapping[str, Any], holder: str, name: str) -> list[dict[str, Any]]:
    body = visual.get("visual")
    objects = body.get(holder) if isinstance(body, dict) else None
    found = objects.get(name) if isinstance(objects, dict) else None
    return [e for e in found if isinstance(e, dict)] if isinstance(found, list) else []


def _check_bookmark_gone_and_switched_off(name: str) -> Callable[[Mapping[str, Any]], list[str]]:
    """The bookmark is gone, and a button kept its action switched off without pointing at
    it."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        problems = []
        if _bookmark_named(parts, name) is not None:
            problems.append(f"Desktop kept the bookmark {name!r}")
        links = [
            entry.get("properties") or {}
            for visual in _visuals(parts)
            if visual.get("visual", {}).get("visualType") == "actionButton"
            for entry in _objects_of(visual, "visualContainerObjects", "visualLink")
        ]
        if not any(
            _literal(p.get("show")) is False and "bookmark" not in p
            for p in links
            if isinstance(p, dict)
        ):
            problems.append("no button kept its action switched off")
        return problems

    return check


def _check_group_flags(expected: Mapping[str, bool]) -> Callable[[Mapping[str, Any]], list[str]]:
    """Each bookmark says isHidden for its (one) group, as expected."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        problems = []
        for name, hidden in expected.items():
            bookmark = _bookmark_named(parts, name)
            if bookmark is None:
                problems.append(f"Desktop dropped the bookmark {name!r}")
                continue
            flags = [
                node.get("isHidden")
                for section in bookmark.get("explorationState", {}).get("sections", {}).values()
                for node in (section.get("visualContainerGroups") or {}).values()
                if isinstance(node, dict)
            ]
            if flags != [hidden]:
                problems.append(f"{name!r} has group flags {flags}, expected [{hidden}]")
        return problems

    return check


def _check_navigator_page_left_out(page_name: str) -> Callable[[Mapping[str, Any]], list[str]]:
    """A page navigator kept showPage false under the selector of the page."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        page = _page_named(parts, page_name)
        if page is None:
            return [f"Desktop dropped the page {page_name!r}"]
        for visual in _visuals(parts):
            if visual.get("visual", {}).get("visualType") != "pageNavigator":
                continue
            for entry in _objects_of(visual, "objects", "pages"):
                props = entry.get("properties") or {}
                if entry.get("selector") == {"id": page.get("name")} and (
                    _literal(props.get("showPage")) is False
                ):
                    return []
        return [f"no page navigator kept {page_name!r} left out"]

    return check


def _check_deselection(name: str) -> Callable[[Mapping[str, Any]], list[str]]:
    """A bookmark navigator kept opening the bookmark on deselection."""

    def check(parts: Mapping[str, Any]) -> list[str]:
        bookmark = _bookmark_named(parts, name)
        if bookmark is None:
            return [f"Desktop dropped the bookmark {name!r}"]
        for visual in _visuals(parts):
            if visual.get("visual", {}).get("visualType") != "bookmarkNavigator":
                continue
            for entry in _objects_of(visual, "objects", "bookmarks"):
                props = entry.get("properties") or {}
                if _literal(props.get("allowDeselectionBookmark")) is True and _literal(
                    props.get("deselectionBookmark")
                ) == bookmark.get("name"):
                    return []
        return [f"no bookmark navigator kept opening {name!r} on deselection"]

    return check


def _probe(
    name: str,
    operations: tuple[Mapping[str, Any], ...],
    expect: str,
    check: Callable[[Mapping[str, Any]], list[str]] | None = None,
) -> ProbeCase:
    return ProbeCase(name=name, family=FAMILY, operations=operations, expect=expect, check=check)


DESKTOP_PROBES: tuple[ProbeCase, ...] = (
    _probe(
        "bookmark_display",
        (
            {"op": "add_bookmark", "name": "Probe charts only", "page": PAGE, "hide": list(CARDS)},
            {"op": "add_bookmark", "name": "Probe everything", "page": PAGE},
        ),
        "View > Bookmarks lists 'Probe charts only' and 'Probe everything'. Clicking 'Probe charts "
        "only' hides the four cards at the top; 'Probe everything' shows them again. No filter or "
        "slicer changes.",
        _check_bookmark("Probe charts only", 4),
    ),
    _probe(
        "bookmark_data_selected_visuals",
        (
            {
                "op": "add_bookmark",
                "name": "Probe slicers",
                "page": PAGE,
                "data": True,
                "display": False,
                "visuals": [SLICER_STORE, SLICER_COUNTRY],
            },
        ),
        "Pick a store in the Store slicer, then click the bookmark 'Probe slicers': both slicers "
        "go back to showing all values (as when the bookmark was made) and the rest of the page "
        "keeps its selection and sort.",
        _check_bookmark("Probe slicers", 0),
    ),
    _probe(
        "bookmark_pane",
        (
            {"op": "add_bookmark", "name": "Probe A", "page": PAGE},
            {"op": "add_bookmark", "name": "Probe B", "page": PAGE, "hide": [TABLE]},
            {"op": "group_bookmarks", "bookmarks": ["Probe A", "Probe B"], "name": "Probe group"},
            {"op": "rename_bookmark", "bookmark": "Probe A", "name": "Probe first"},
            {"op": "move_bookmark", "bookmark": "Probe B", "position": 1},
            {"op": "set_bookmark_visual_hidden", "bookmark": "Probe first", "visual": CHART_TOTAL},
            {
                "op": "update_bookmark",
                "bookmark": "Probe B",
                "recapture": False,
                "current_page": False,
            },
        ),
        "The Bookmarks pane shows a group 'Probe group' holding 'Probe B' then 'Probe first'. "
        "'Probe first' hides the Total MTD Sales chart; 'Probe B' hides the table.",
        _check_bookmark("Probe first", 1),
    ),
    _probe(
        "bookmark_ungroup",
        (
            {"op": "add_bookmark", "name": "Probe loose 1", "page": PAGE},
            {"op": "add_bookmark", "name": "Probe loose 2", "page": PAGE, "hide": [CHART_TOTAL]},
            {
                "op": "group_bookmarks",
                "bookmarks": ["Probe loose 1", "Probe loose 2"],
                "name": "Probe temporary",
            },
            {"op": "ungroup_bookmarks", "group": "Probe temporary"},
        ),
        "The Bookmarks pane lists 'Probe loose 1' and 'Probe loose 2' at the top level, with no "
        "group.",
        _check_bookmark("Probe loose 2", 1),
    ),
    _probe(
        "bookmark_delete_referenced",
        (
            {"op": "add_bookmark", "name": "Probe gone", "page": PAGE},
            {
                "op": "add_button",
                "page": PAGE,
                "kind": "bookmark",
                "action": {"type": "bookmark", "bookmark": "Probe gone"},
                "x": 20,
                "y": 20,
            },
            {"op": "delete_bookmark", "bookmark": "Probe gone", "cleanup_references": True},
        ),
        "No bookmark 'Probe gone'; a bookmark button at the top left does nothing when clicked "
        "(its action is off) and Desktop shows no error.",
        _check_bookmark_gone_and_switched_off("Probe gone"),
    ),
    _probe(
        "bookmark_groups",
        (
            # Page 1 has no visual group of its own: the extras family's group_visuals makes one.
            {"op": "group_visuals", "visuals": list(CARDS), "name": "Probe cards"},
            {
                "op": "add_bookmark",
                "name": "Probe cards hidden",
                "page": PAGE,
                "hide": ["Probe cards"],
            },
            {"op": "add_bookmark", "name": "Probe cards shown", "page": PAGE},
        ),
        "The Selection pane shows a group 'Probe cards' holding the four cards. Clicking "
        "'Probe cards hidden' hides the group and its cards (the group's eye is off); 'Probe "
        "cards shown' shows them again.",
        _check_group_flags({"Probe cards hidden": True, "Probe cards shown": False}),
    ),
    _probe(
        "navigator_deselection",
        (
            {"op": "add_bookmark", "name": "Probe everyone", "page": PAGE},
            {"op": "add_bookmark", "name": "Probe no table", "page": PAGE, "hide": [TABLE]},
            {"op": "add_navigator", "page": PAGE, "kind": "bookmark", "x": 20, "y": 1120},
            {
                "op": "set_navigator",
                "visual": "bookmark navigator",
                "deselection_bookmark": "Probe everyone",
            },
        ),
        "Format the bookmark navigator > Bookmarks: 'Allow deselection' is on and 'Launch "
        "bookmark on deselection' is 'Probe everyone'. Ctrl+click 'Probe no table' (the table "
        "hides), then Ctrl+click it again: 'Probe everyone' is applied and the table is back.",
        _check_deselection("Probe everyone"),
    ),
    _probe(
        "buttons",
        (
            {"op": "add_page", "name": "Probe target"},
            {
                "op": "add_bookmark",
                "name": "Probe button bookmark",
                "page": PAGE,
                "hide": list(CARDS),
            },
            {"op": "add_button", "page": PAGE, "kind": "back", "x": 20, "y": 20},
            {
                "op": "add_button",
                "page": PAGE,
                "kind": "blank",
                "text": "Go",
                "action": {"type": "page", "page": "Probe target"},
                "tooltip": "Open the probe page",
                "x": 130,
                "y": 20,
            },
            {
                "op": "add_button",
                "page": PAGE,
                "kind": "bookmark",
                "action": {"type": "bookmark", "bookmark": "Probe button bookmark"},
                "x": 240,
                "y": 20,
            },
            {
                "op": "add_button",
                "page": PAGE,
                "kind": "information",
                "action": {"type": "web_url", "url": "https://learn.microsoft.com/power-bi/"},
                "x": 350,
                "y": 20,
            },
            {"op": "add_button", "page": PAGE, "kind": "help", "x": 460, "y": 20},
            {"op": "add_button", "page": PAGE, "kind": "qna", "x": 570, "y": 20},
            {"op": "add_button", "page": PAGE, "kind": "left_arrow", "x": 680, "y": 20},
            {"op": "add_button", "page": PAGE, "kind": "right_arrow", "x": 790, "y": 20},
            {"op": "add_button", "page": PAGE, "kind": "reset", "x": 900, "y": 20},
        ),
        "Nine buttons across the top of page 1 with Desktop's icons: back, blank 'Go' (tooltip "
        "'Open the probe page'), bookmark, information, help (question mark), Q&A, left and "
        "right arrows, reset. "
        "Ctrl+click: 'Go' opens 'Probe target', the bookmark button hides the cards, information "
        "opens the Power BI docs, Q&A opens Q&A.",
        _check_visual_property(
            ("visual", "visualContainerObjects", "visualLink"), "PageNavigation"
        ),
    ),
    _probe(
        "slicer_buttons",
        (
            {"op": "add_button", "page": PAGE, "kind": "apply_all_slicers", "x": 20, "y": 20},
            {"op": "add_button", "page": PAGE, "kind": "clear_all_slicers", "x": 240, "y": 20},
        ),
        "Two 200 x 50 buttons at the top left say 'Apply all slicers' and 'Clear all slicers' "
        "(centred, no icon); hovering shows Desktop's default tooltips. Pick a store: the pick "
        "waits until 'Apply all slicers' is Ctrl+clicked; 'Clear all slicers' clears every slicer "
        "of the page.",
        _check_visual_property(
            ("visual", "visualContainerObjects", "visualLink"), "ApplyAllSlicers"
        ),
    ),
    _probe(
        "navigators",
        (
            {"op": "add_page", "name": "Probe nav A"},
            {"op": "add_page", "name": "Probe nav B", "after": "Probe nav A"},
            {"op": "set_page_hidden", "page": "Probe nav B", "hidden": True},
            {"op": "add_bookmark", "name": "Probe nav 1", "page": PAGE},
            {"op": "add_bookmark", "name": "Probe nav 2", "page": PAGE, "hide": [TABLE]},
            {
                "op": "group_bookmarks",
                "bookmarks": ["Probe nav 1", "Probe nav 2"],
                "name": "Probe nav group",
            },
            {"op": "add_navigator", "page": PAGE, "kind": "page", "x": 20, "y": 1120},
            {"op": "set_navigator", "visual": "page navigator", "show_hidden_pages": False},
            {
                "op": "add_navigator",
                "page": PAGE,
                "kind": "bookmark",
                "bookmark_group": "Probe nav group",
                "rows": 1,
                "columns": 2,
                "x": 700,
                "y": 1120,
            },
        ),
        "At the bottom of page 1 a page navigator lists '1' and 'Probe nav A' but not the hidden "
        "'Probe nav B'; next to it a bookmark navigator in a 1 x 2 grid lists 'Probe nav 1' and "
        "'Probe nav 2' only.",
        _check_visual_property(("visual", "visualType"), "bookmarkNavigator"),
    ),
    _probe(
        "navigator_pages",
        (
            {"op": "add_page", "name": "Probe listed"},
            {"op": "add_page", "name": "Probe left out"},
            {"op": "add_navigator", "page": PAGE, "kind": "page", "x": 20, "y": 1120},
            {"op": "set_navigator", "visual": "page navigator", "hide_pages": ["Probe left out"]},
        ),
        "The page navigator at the bottom of page 1 lists '1' and 'Probe listed' but not 'Probe "
        "left out' (Format > Pages > Show: 'Probe left out' is off).",
        _check_navigator_page_left_out("Probe left out"),
    ),
    _probe(
        "drillthrough",
        (
            {"op": "add_page", "name": "Probe drillthrough"},
            {
                "op": "set_drillthrough",
                "page": "Probe drillthrough",
                "fields": [{"table": "DimStore", "name": "StoreName"}],
                "keep_all_filters": False,
            },
            {
                "op": "add_button",
                "page": PAGE,
                "kind": "blank",
                "text": "Store details",
                "x": 20,
                "y": 20,
            },
            {
                "op": "set_button_action",
                "visual": "Store details",
                "action": {"type": "drillthrough", "page": "Probe drillthrough"},
                "tooltip": "Pick one store first",
            },
        ),
        "Page 'Probe drillthrough' shows a Drill through well with StoreName, 'Keep all filters' "
        "off, and a Back button at its top left. On page 1, right-click a store in the Store "
        "slicer or table: Drill through > Probe drillthrough is offered. The 'Store details' "
        "button (tooltip 'Pick one store first') is enabled once a single store is selected.",
        _check_page_type("Probe drillthrough", "Drillthrough"),
    ),
    _probe(
        "tooltip_page",
        (
            {"op": "add_page", "name": "Probe tooltip"},
            {
                "op": "set_tooltip_page",
                "page": "Probe tooltip",
                "fields": [{"table": "Clustered Employees", "name": "Clustered Employees"}],
            },
            {"op": "set_visual_tooltip", "visual": CHART_TOTAL, "page": "Probe tooltip"},
        ),
        "'Probe tooltip' is hidden, 320 x 240, of type Tooltip. Hovering a bar of Total MTD Sales "
        "shows the (empty) tooltip page instead of the default tooltip.",
        _check_page_type("Probe tooltip", "Tooltip"),
    ),
    _probe(
        "clear_page_type",
        (
            {"op": "add_page", "name": "Probe was drillthrough"},
            {
                "op": "set_drillthrough",
                "page": "Probe was drillthrough",
                "fields": [{"table": "DimChannel", "name": "ChannelName"}],
                "back_button": False,
            },
            {"op": "clear_page_type", "page": "Probe was drillthrough"},
        ),
        "'Probe was drillthrough' is an ordinary page: its Drill through well is empty and its "
        "Filters pane has no drillthrough filter.",
        _check_page_type("Probe was drillthrough", None),
    ),
    _probe(
        "interactions",
        (
            {
                "op": "set_interaction",
                "source": SLICER_COUNTRY,
                "targets": [CHART_TOTAL],
                "type": "none",
            },
            {
                "op": "set_interaction",
                "source": CHART_TOTAL,
                "targets": [CHART_AVG],
                "type": "filter",
            },
            {
                "op": "set_interaction",
                "source": CHART_TOTAL,
                "targets": [CHART_CHANGE],
                "type": "highlight",
            },
            {"op": "set_interaction", "source": CHART_TOTAL, "targets": [TABLE], "type": "none"},
            {"op": "set_interaction", "source": SLICER_STORE, "type": "none"},
            {"op": "reset_interactions", "visual": SLICER_STORE},
        ),
        "Format > Edit interactions with Total MTD Sales selected: Avg MTD Sales by Agents shows "
        "Filter, Avg MTD % change shows Highlight, the table shows None. With the Country slicer "
        "selected, Total MTD Sales shows None. The Store slicer still filters every visual.",
        _check_interactions(
            {
                (SLICER_COUNTRY, CHART_TOTAL): "NoFilter",
                (CHART_TOTAL, CHART_AVG): "DataFilter",
                (CHART_TOTAL, CHART_CHANGE): "HighlightFilter",
                (CHART_TOTAL, TABLE): "NoFilter",
            }
        ),
    ),
    _probe(
        "sync_slicers",
        (
            {"op": "add_page", "name": "Probe sync A"},
            {"op": "add_page", "name": "Probe sync B"},
            {
                "op": "sync_slicer",
                "visual": SLICER_STORE,
                "pages": ["Probe sync A"],
                "hidden_on": ["Probe sync B"],
                "group": "Probe stores",
            },
            {"op": "sync_slicer", "visual": SLICER_CHANNEL, "pages": ["Probe sync A"]},
            {
                "op": "unsync_slicer",
                "visual": SLICER_CHANNEL,
                "pages": ["Probe sync A"],
                "delete_copies": True,
            },
        ),
        "View > Sync slicers with the Store slicer selected: 'Probe sync A' is synced and "
        "visible, 'Probe sync B' synced but not visible. Picking a store on page 1 shows it "
        "picked on 'Probe sync A' too. 'Probe sync A' has no Channel slicer, and the Channel "
        "slicer on page 1 is not synced.",
        _check_sync("Probe stores", 3),
    ),
    _probe(
        "drill",
        (
            {
                "op": "set_fields",
                "visual": CHART_DISCOUNT,
                "role": "Category",
                "mode": "add",
                "fields": [{"table": "DimChannel", "name": "ChannelName"}],
            },
            {"op": "drill_visual", "visual": CHART_DISCOUNT, "action": "expand_all"},
            {"op": "set_drill_filter", "visual": CHART_DISCOUNT, "filter_other_visuals": False},
            {"op": "set_drill_filter", "filter_other_visuals": False},
        ),
        "Avg Discount per Transaction shows 'Others Online'-style labels (expanded one level: "
        "cluster and channel together); its drill buttons in the header are active. Drilling in it "
        "does not filter the other visuals (Format > Edit interactions > drilling).",
        _check_active(CHART_DISCOUNT, [True, True]),
    ),
)
