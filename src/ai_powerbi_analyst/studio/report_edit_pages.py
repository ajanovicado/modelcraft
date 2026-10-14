"""Page operations: rename, add, duplicate, delete, move, resize and hide pages.

What each operation guarantees beyond the JSON it writes:

- Pages are found by id, display name or 1-based position (``PbirDocument.resolve_page``); a
  miss lists the pages. A page is referenced by its folder name from buttons, tooltips,
  drillthrough links and bookmarks, so ``RenamePage`` only changes the display name and
  ``DeletePage`` never leaves one of those names dangling (P23).
- ``DuplicatePage`` makes an independent page: new visual names, interactions that follow
  them, new filter names and a new page-binding identity, placed after the source (P23).
- ``SetPageSize`` scales the visuals with the page unless told not to, and refuses rather
  than push a visual off the page (P35).

Page backgrounds and the report theme are styling, which Studio does not change (the
operations that did were removed; chat refuses such a request).

To add an operation: define its class here, add it to ``PageOperation``, add its handler to
``PAGE_HANDLERS``, and import both in the ``pages`` block of ``report_edits``.
"""

from __future__ import annotations

import copy

from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_edit_context import (
    Handler,
    HandlerResult,
    ModelInfo,
    NeedsConfirmation,
    _label,
    _page_bounds,
)
from .report_edit_ops import (
    AddPage,
    DeletePage,
    DuplicatePage,
    MovePage,
    RenamePage,
    SetPageHidden,
    SetPageSize,
    operation_classes,
)
from .report_refs import clear_page_references, remap_duplicate_page, remove_page_references

PageOperation = (
    RenamePage | AddPage | DuplicatePage | DeletePage | MovePage | SetPageSize | SetPageHidden
)
PAGE_OPERATIONS = operation_classes(PageOperation)

_REGISTERED = "RegisteredResources"
_MAX_LISTED = 8


def _plural(count: int, noun: str, many: str | None = None) -> str:
    return f"{count} {noun if count == 1 else many or noun + 's'}"


def _listed(items: list[str]) -> str:
    shown = items[:_MAX_LISTED]
    more = len(items) - len(shown)
    return "; ".join(shown) + (f"; and {more} more" if more else "")


def _taken(document: PbirDocument, name: str, besides: PageDoc | None = None) -> bool:
    return any(
        p.display_name.casefold() == name.casefold() and p is not besides
        for p in document.pages.values()
    )


# --- rename, add, duplicate, delete, move --------------------------------------------------------


def _apply_rename_page(document: PbirDocument, op: RenamePage, model: ModelInfo) -> str:
    page = document.resolve_page(op.page)
    old = page.display_name
    name = op.name.strip()
    if not name:
        raise DocumentError("A page needs a name")
    if _taken(document, name, page):
        raise DocumentError(f"There is already a page named {name!r}")
    # Buttons, tooltips and bookmarks refer to a page by its folder name, which stays.
    page.data["displayName"] = name
    document.touch_page(page)
    return f"Renamed page {old!r} to {name!r}."


def _new_page(document: PbirDocument, name: str, width: float, height: float) -> PageDoc:
    page_name = "ReportSection" + document.new_name(set(document.pages))[:20]
    return PageDoc(
        page_name,
        {
            "$schema": document.schema("page"),
            "name": page_name,
            "displayName": name,
            "displayOption": "FitToPage",
            "height": height,
            "width": width,
        },
    )


def _insert_page(document: PbirDocument, page: PageDoc, after: str | None) -> None:
    order = list(document.pages)
    index = order.index(document.resolve_page(after).name) + 1 if after else len(order)
    order.insert(index, page.name)
    pages = {**document.pages, page.name: page}
    document.pages = {name: pages[name] for name in order}
    document.pages_meta["pageOrder"] = order
    document.touch_pages()
    document.touch_page(page)


def _apply_add_page(document: PbirDocument, op: AddPage, model: ModelInfo) -> str:
    name = op.name.strip()
    if not name:
        raise DocumentError("A page needs a name")
    if _taken(document, name):
        raise DocumentError(f"There is already a page named {name!r}")
    page = _new_page(document, name, op.width, op.height)
    _insert_page(document, page, op.after)
    return f"Added page {name!r}."


def _copy_name(document: PbirDocument, source: str) -> str:
    """'Sales (copy)', then 'Sales (copy 2)'... the first display name not yet used."""
    candidate, count = f"{source} (copy)", 1
    while _taken(document, candidate):
        count += 1
        candidate = f"{source} (copy {count})"
    return candidate


def _apply_duplicate_page(document: PbirDocument, op: DuplicatePage, model: ModelInfo) -> str:
    source = document.resolve_page(op.page)
    name = op.name.strip() if op.name else _copy_name(document, source.display_name)
    if not name:
        raise DocumentError("A page needs a name")
    if _taken(document, name):
        raise DocumentError(f"There is already a page named {name!r}")
    page = _new_page(
        document, name, float(source.data.get("width", 1280)), float(source.data.get("height", 720))
    )
    data = copy.deepcopy(source.data)
    data.update({"name": page.name, "displayName": name})
    page.data = data
    # The copy joins the report before its visuals are made: the remapping reads the report's
    # visual, filter and page-binding names to mint ones nothing else uses.
    _insert_page(document, page, source.name)
    remap_duplicate_page(document, source, page)
    return f"Duplicated page {source.display_name!r} as {name!r}."


def _referrer_text(page: PageDoc, referrers: list[str]) -> str:
    return (
        f"Page {page.display_name!r} is still used: {_listed(referrers)}. Deleting it switches "
        f"{'that' if len(referrers) == 1 else 'those'} off, and a bookmark that only covers "
        f"this page is deleted too."
    )


def _image_value(objects: dict[str, object], object_name: str) -> dict[str, object] | None:
    entries = objects.get(object_name)
    for entry in entries if isinstance(entries, list) else []:
        props = entry.get("properties") if isinstance(entry, dict) else None
        image = props.get("image") if isinstance(props, dict) else None
        inner = image.get("image") if isinstance(image, dict) else None
        if isinstance(inner, dict):
            return inner
    return None


def _image_item(image: dict[str, object] | None) -> str | None:
    url = image.get("url") if image else None
    inner = url.get("expr") if isinstance(url, dict) else None
    item = inner.get("ResourcePackageItem") if isinstance(inner, dict) else None
    name = item.get("ItemName") if isinstance(item, dict) else None
    return name if isinstance(name, str) else None


def _drop_image_if_unused(document: PbirDocument, item: str | None) -> None:
    """Remove a registered image nothing references any more (page, visual or bookmark)."""
    if item and not document.resource_uses(_REGISTERED, item):
        document.unregister_resource(_REGISTERED, item)


def _apply_delete_page(document: PbirDocument, op: DeletePage, model: ModelInfo) -> str:
    page = document.resolve_page(op.page)
    if len(document.pages) == 1:
        raise DocumentError("A report needs at least one page")
    referrers = remove_page_references(document, page.name)
    changes: list[str] = []
    if referrers:
        if not op.cleanup_references:
            raise NeedsConfirmation("remove_page_references", _referrer_text(page, referrers))
        changes = clear_page_references(document, page.name)
        left = remove_page_references(document, page.name)
        if left:
            raise DocumentError(
                f"Page {page.display_name!r} could not be deleted cleanly: {_listed(left)}"
            )
    objects = page.data.get("objects")
    pictures = (
        {_image_item(_image_value(objects, name)) for name in ("background", "outspace")}
        if isinstance(objects, dict)
        else set()
    )
    document.remove_page(page)
    # The page's own background pictures go with it unless another page or visual shows them
    for item in sorted(i for i in pictures if i):
        _drop_image_if_unused(document, item)
    return " ".join([f"Deleted page {page.display_name!r}.", *changes])


def _apply_move_page(document: PbirDocument, op: MovePage, model: ModelInfo) -> str:
    page = document.resolve_page(op.page)
    order = [p for p in document.pages if p != page.name]
    order.insert(min(op.index, len(order)), page.name)
    document.pages = {name: document.pages[name] for name in order}
    document.pages_meta["pageOrder"] = order
    document.touch_pages()
    return f"Moved page {page.display_name!r} to position {order.index(page.name) + 1}."


# --- size -------------------------------------------------------------------------------------


def _pixels(value: float) -> int | float:
    """A stored pixel value: four decimals, and a whole number stays an int as Desktop writes it."""
    rounded = round(value, 4)
    return int(rounded) if rounded.is_integer() else rounded


def _scale_box(position: dict[str, object], fx: float, fy: float) -> None:
    for key, factor in (("x", fx), ("y", fy), ("width", fx), ("height", fy)):
        value = position.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            position[key] = _pixels(float(value) * factor)


def _trim_to_page(position: dict[str, object], width: float, height: float) -> None:
    """Take back the rounding that pushed a flush edge a hair past the page."""
    for start, size, limit in (("x", "width", width), ("y", "height", height)):
        origin, extent = position.get(start), position.get(size)
        if isinstance(origin, (int, float)) and isinstance(extent, (int, float)):
            over = float(origin) + float(extent) - limit
            if 0 < over <= 0.001:
                position[size] = _pixels(float(extent) - over)


def _visual_name(page: PageDoc, name: str) -> str:
    visual = page.visuals[name]
    group = visual.get("visualGroup")
    if isinstance(group, dict):
        return f"the group {group.get('displayName') or name[:8]!r}"
    return _label(visual, name[:8])


def _outside(page: PageDoc, width: float, height: float) -> list[str]:
    """What would stick out of a width x height page, in page pixels (group members add the
    origin of their group), described in plain English."""
    found: list[str] = []
    for name in page.visuals:
        x, y, w, h = page.box(name)
        problems: list[str] = []
        if x + w > width + 0.5:
            problems.append(f"reaches {x + w:.0f} across a page {width:g} wide")
        if y + h > height + 0.5:
            problems.append(f"reaches {y + h:.0f} down a page {height:g} high")
        if problems:
            found.append(f"{_visual_name(page, name)} {' and '.join(problems)}")
    return found


def _apply_set_page_size(
    document: PbirDocument, op: SetPageSize, model: ModelInfo
) -> str | HandlerResult:
    page = document.resolve_page(op.page)
    old_width, old_height = _page_bounds(page)
    scaled = 0
    warnings: list[str] = []
    if not op.scale:
        outside = _outside(page, op.width, op.height)
        if outside:
            raise DocumentError(
                f"A {op.width:g} x {op.height:g} page is too small to hold "
                f"{_plural(len(outside), 'visual')} on page {page.display_name!r}: "
                f"{_listed(outside)}. Scale the visuals with the page, or pick a larger size."
            )
    elif (op.width, op.height) != (old_width, old_height):
        fx, fy = op.width / old_width, op.height / old_height
        # A group member's stored box is relative to its group, and page position is the sum
        # of the group origins, so scaling every stored box by the same factors scales page
        # positions by them too: a group and its members stay in proportion and members stay
        # inside the group. Leaving members alone would shrink the group around them.
        for name, visual in page.visuals.items():
            position = visual.get("position")
            if isinstance(position, dict):
                _scale_box(position, fx, fy)
                if "parentGroupName" not in visual:
                    _trim_to_page(position, op.width, op.height)
            document.touch_visual(page, name)
            scaled += "visualGroup" not in visual
        if abs(fx - fy) > 0.005 and scaled:
            warnings.append(
                f"The new page has a different shape (width x{fx:.2f}, height x{fy:.2f}), so "
                "the visuals were stretched to match. Check the layout in Power BI."
            )
    page.data["width"], page.data["height"] = op.width, op.height
    if op.display:
        page.data["displayOption"] = op.display
    document.touch_page(page)
    summary = f"Resized page {page.display_name!r} to {op.width:g} x {op.height:g}"
    summary += f" and scaled its {_plural(scaled, 'visual')} with it." if scaled else "."
    return HandlerResult(summary, warnings=warnings) if warnings else summary


def _apply_set_page_hidden(
    document: PbirDocument, op: SetPageHidden, model: ModelInfo
) -> str | HandlerResult:
    page = document.resolve_page(op.page)
    if op.hidden:
        page.data["visibility"] = "HiddenInViewMode"
    else:
        page.data.pop("visibility", None)
    document.touch_page(page)
    summary = f"{'Hid' if op.hidden else 'Showed'} page {page.display_name!r}."
    if op.hidden and all(
        p.data.get("visibility") == "HiddenInViewMode" for p in document.pages.values()
    ):
        return HandlerResult(
            summary,
            warnings=["Every page is hidden now, so readers of the report will see none."],
        )
    return summary


PAGE_HANDLERS: dict[str, Handler] = {
    "rename_page": _apply_rename_page,
    "add_page": _apply_add_page,
    "duplicate_page": _apply_duplicate_page,
    "delete_page": _apply_delete_page,
    "move_page": _apply_move_page,
    "set_page_size": _apply_set_page_size,
    "set_page_hidden": _apply_set_page_hidden,
}
