"""An imported report's PBIR definition as an editable document.

It covers what a report author edits: report.json (theme, resources, settings),
pages.json (order, active page), each page.json and each visual.json, and the bookmarks.
A visual folder's other parts (mobile.json) and every part outside Report/definition stay
in the package untouched. Changes are written back part by part; deleting a visual or a
page removes its whole folder, as Desktop does, and deleting a bookmark removes its file
and its entry in the bookmark list.

A report with no DataModel part is a thin report: its data lives in a shared dataset, so
Studio can recognise it (``is_thin``) and refuse or flag what it cannot verify.
"""

from __future__ import annotations

import copy
import re
import secrets
from collections.abc import Collection, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple, overload

from .pbir_expr import decode, theme_palette
from .pbix_package import PackageError, PbixPackage, decode_json

ROOT = "Report/definition/"
REPORT = ROOT + "report.json"
PAGES = ROOT + "pages/pages.json"
BOOKMARKS = ROOT + "bookmarks/"
BOOKMARKS_META = BOOKMARKS + "bookmarks.json"
# The version Desktop 2.157 ships and validates bookmarks.json with (desktop.schema.json.0).
BOOKMARKS_METADATA_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "bookmarksMetadata/1.0.0/schema.json"
)
EXTENSIONS = ROOT + "reportExtensions.json"
_RESOURCES = "Report/StaticResources/"
_BOOKMARK_FILE = re.compile(re.escape(BOOKMARKS) + r"([^/]+)\.bookmark\.json")
_BOOKMARK_KEY = re.compile(r"[A-Za-z0-9_\-]{1,100}")
_RESOURCE_PACKAGES = ("RegisteredResources", "SharedResources")

PartKind = Literal["report", "pages", "page", "visual", "bookmark"]


class DocumentError(ValueError):
    """The report cannot be edited as asked."""


class Unlisted(NamedTuple):
    """What the bookmark list lost when a bookmark left it, besides the bookmark: groups left
    empty (their display names; Desktop drops empty groups) and the bookmark navigators
    (page, visual) that listed one of them and now list every bookmark."""

    groups: tuple[str, ...]
    navigators: tuple[tuple[str, str], ...]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """``override`` over ``base``: dicts merge per key, lists and scalars are replaced."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _uses_resource(node: Any, package_name: str, item_name: str) -> bool:
    if isinstance(node, dict):
        item = node.get("ResourcePackageItem")
        if (
            isinstance(item, dict)
            and item.get("PackageName") == package_name
            and item.get("ItemName") == item_name
        ):
            return True
        return any(_uses_resource(v, package_name, item_name) for v in node.values())
    if isinstance(node, list):
        return any(_uses_resource(v, package_name, item_name) for v in node)
    return False


@dataclass
class PageDoc:
    name: str
    data: dict[str, Any]
    visuals: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def folder(self) -> str:
        return f"{ROOT}pages/{self.name}/"

    @property
    def display_name(self) -> str:
        return str(self.data.get("displayName", self.name))

    def box(self, visual: str) -> tuple[float, float, float, float]:
        """(x, y, width, height) of a visual in page pixels.

        A group member's stored position is relative to its group's origin (measured in
        Desktop 2.157) and nested groups add up, so the page position is the sum of the
        origins of every group above it. The stored JSON keeps the relative numbers.
        """
        found = self.visuals[visual]
        position = found.get("position") or {}
        x, y = float(position.get("x", 0)), float(position.get("y", 0))
        seen = {visual}
        parent = found.get("parentGroupName")
        while isinstance(parent, str) and parent in self.visuals and parent not in seen:
            seen.add(parent)
            above = self.visuals[parent]
            origin = above.get("position") or {}
            x += float(origin.get("x", 0))
            y += float(origin.get("y", 0))
            parent = above.get("parentGroupName")
        return x, y, float(position.get("width", 0)), float(position.get("height", 0))


class PbirDocument:
    def __init__(self, package: PbixPackage) -> None:
        if not package.has(REPORT):
            if package.has("Report/Layout"):
                raise DocumentError(
                    "This report uses Power BI's older report format. Open it in Power BI "
                    "Desktop and save it once (File > Save) so it uses the PBIR format, then "
                    "import it again to edit it in Studio."
                )
            raise DocumentError("This PBIX has no editable report definition")
        self.package = package
        self._dirty: set[str] = set()
        self._removed_folders: set[str] = set()
        self._removed_parts: set[str] = set()
        visual_file = re.compile(re.escape(ROOT) + r"pages/([^/]+)/visuals/([^/]+)/visual\.json")
        names = package.names()
        page_parts: dict[str, str] = {}
        visual_parts: list[tuple[str, str, str]] = []
        self._bookmark_parts: dict[str, str] = {}
        for name in names:
            if name.startswith(ROOT + "pages/") and name.endswith("/page.json"):
                page_name = name[len(ROOT + "pages/") : -len("/page.json")]
                if "/" not in page_name:
                    page_parts[page_name] = name
            found = visual_file.fullmatch(name)
            if found:
                visual_parts.append((found.group(1), found.group(2), name))
            bookmark = _BOOKMARK_FILE.fullmatch(name)
            if bookmark:
                self._bookmark_parts[bookmark.group(1)] = name
        visual_parts = [v for v in visual_parts if v[0] in page_parts]
        wanted = [REPORT] + ([PAGES] if package.has(PAGES) else [])
        wanted += list(page_parts.values()) + [part for _, _, part in visual_parts]
        loaded = {name: decode_json(data) for name, data in package.read_parts(wanted).items()}
        self.report: dict[str, Any] = loaded[REPORT]
        self.pages_meta: dict[str, Any] = loaded.get(PAGES, {"pageOrder": []})
        pages = {p: PageDoc(p, loaded[part]) for p, part in page_parts.items()}
        for page_name, visual_name, part in visual_parts:
            pages[page_name].visuals[visual_name] = loaded[part]
        order = [p for p in self.pages_meta.get("pageOrder", []) if p in pages]
        order += [p for p in pages if p not in order]
        self.pages: dict[str, PageDoc] = {p: pages[p] for p in order}
        self._bookmarks: dict[str, dict[str, Any]] | None = None
        self._bookmarks_meta: dict[str, Any] | None = None
        self._extensions: dict[str, Any] | None = None

    # --- thin reports --------------------------------------------------------------------

    @property
    def has_model(self) -> bool:
        """True when the file carries its own data model (an ordinary PBIX)."""
        return self.package.has("DataModel")

    @property
    def is_thin(self) -> bool:
        """A report over a shared dataset: it has no DataModel part of its own."""
        return not self.has_model

    # --- lookup ----------------------------------------------------------------------

    def page(self, key: str) -> PageDoc:
        """A page by its id or its display name (case-insensitive)."""
        if key in self.pages:
            return self.pages[key]
        matches = [p for p in self.pages.values() if p.display_name.casefold() == key.casefold()]
        if len(matches) == 1:
            return matches[0]
        raise DocumentError(f"There is no page named {key!r}")

    def page_list(self) -> str:
        """The pages in order for a message: ``1. 'Overview', 2. 'Details'``."""
        return ", ".join(f"{i}. {p.display_name!r}" for i, p in enumerate(self.pages.values(), 1))

    def resolve_page(self, key: str) -> PageDoc:
        """A page by its id, its display name (case-insensitive) or its 1-based position.

        A name wins over a position, so a page called "2" is found by "2". Unlike ``page``,
        a failure says why and lists the pages, so the user can pick one.
        """
        text = str(key).strip()
        if text in self.pages:
            return self.pages[text]
        matches = [p for p in self.pages.values() if p.display_name.casefold() == text.casefold()]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DocumentError(
                f"{len(matches)} pages are named {text!r}; say which one by its number "
                f"(the pages are: {self.page_list()})"
            )
        if text.isdecimal() and 1 <= int(text) <= len(self.pages):
            return list(self.pages.values())[int(text) - 1]
        raise DocumentError(f"There is no page {text!r}. The pages are: {self.page_list()}")

    @overload
    def page_index(self) -> dict[str, int]: ...

    @overload
    def page_index(self, key: PageDoc | str) -> int: ...

    def page_index(self, key: PageDoc | str | None = None) -> dict[str, int] | int:
        """Position in the page order: every page's (id -> index), or one page's index."""
        positions = {name: index for index, name in enumerate(self.pages)}
        if key is None:
            return positions
        return positions[key.name if isinstance(key, PageDoc) else self.page(key).name]

    def visual(self, visual_id: str) -> tuple[PageDoc, dict[str, Any]]:
        for page in self.pages.values():
            if visual_id in page.visuals:
                return page, page.visuals[visual_id]
        raise DocumentError(f"There is no visual {visual_id!r} in this report")

    def all_visuals(self) -> list[tuple[PageDoc, str, dict[str, Any]]]:
        return [(p, n, v) for p in self.pages.values() for n, v in p.visuals.items()]

    # --- names and schemas -------------------------------------------------------------

    def new_name(self, used: set[str]) -> str:
        """A 20-hex-character name like Desktop's, unused in ``used``."""
        while True:
            candidate = secrets.token_hex(10)
            if candidate not in used:
                return candidate

    def visual_names(self) -> set[str]:
        return {n for p in self.pages.values() for n in p.visuals}

    def schema(self, kind: str) -> str:
        """The $schema the report already uses for this part kind (visualContainer, page)."""
        if kind == "visualContainer":
            for _, _, visual in self.all_visuals():
                if isinstance(visual.get("$schema"), str):
                    return str(visual["$schema"])
        if kind == "page":
            for page in self.pages.values():
                if isinstance(page.data.get("$schema"), str):
                    return str(page.data["$schema"])
        return (
            "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
            + {"visualContainer": "visualContainer/2.7.0", "page": "page/2.1.0"}[kind]
            + "/schema.json"
        )

    # --- themes ------------------------------------------------------------------------

    def _resolve_theme(self, collection_key: str) -> tuple[str, str, dict[str, Any]] | None:
        """(package type, part name, theme) of a themeCollection entry.

        A gallery theme Desktop copied in lives in SharedResources even when it is the
        custom theme, and Studio's own lives in RegisteredResources. The entry's ``type``
        says which package to look in first; the other one is the fallback, so a report
        whose ``type`` and item disagree still resolves.
        """
        entry = self.report.get("themeCollection", {}).get(collection_key)
        if not isinstance(entry, dict) or not entry.get("name"):
            return None
        preferred = entry.get("type", "SharedResources")
        packages = [
            p
            for p in self.report.get("resourcePackages", [])
            if isinstance(p, dict) and p.get("type") in _RESOURCE_PACKAGES
        ]
        for package in sorted(packages, key=lambda p: p.get("type") != preferred):
            package_type = str(package["type"])
            for item in package.get("items", []):
                if not isinstance(item, dict):
                    continue
                if item.get("name") != entry["name"] or not isinstance(item.get("path"), str):
                    continue
                part = f"{_RESOURCES}{package.get('name', package_type)}/{item['path']}"
                try:
                    theme = self.package.read_json(part)
                except (KeyError, PackageError, ValueError):
                    continue
                if isinstance(theme, dict):
                    return package_type, part, theme
        return None

    def _theme_item(self, collection_key: str) -> tuple[str, dict[str, Any]] | None:
        found = self._resolve_theme(collection_key)
        return (found[1], found[2]) if found else None

    def base_theme(self) -> dict[str, Any]:
        found = self._theme_item("baseTheme")
        return found[1] if found else {}

    def custom_theme(self) -> tuple[str, dict[str, Any]] | None:
        """(part name, theme) of the report's custom theme, wherever it is stored."""
        return self._theme_item("customTheme")

    def custom_theme_location(self) -> tuple[str, str] | None:
        """(package, part name) of the custom theme, or None without one.

        The package is "RegisteredResources" or "SharedResources".
        """
        found = self._resolve_theme("customTheme")
        return (found[0], found[1]) if found else None

    def theme(self) -> dict[str, Any]:
        """The effective theme: the custom theme merged over the base theme.

        Dicts merge per key, so a custom theme that sets one entry of ``textClasses`` or
        ``visualStyles`` keeps the base theme's other entries, as Desktop does. Lists
        (dataColors) and scalars are replaced by the custom value.
        """
        merged = copy.deepcopy(self.base_theme())
        custom = self.custom_theme()
        if custom:
            merged = _deep_merge(merged, custom[1])
        return merged

    def palette(self) -> list[str]:
        return theme_palette(self.theme())

    # --- change tracking ------------------------------------------------------------------

    def touch_report(self) -> None:
        self._dirty.add(REPORT)

    def touch_pages(self) -> None:
        self._dirty.add(PAGES)

    def touch_page(self, page: PageDoc) -> None:
        self._dirty.add(page.folder + "page.json")

    def touch_visual(self, page: PageDoc, visual_id: str) -> None:
        self._dirty.add(f"{page.folder}visuals/{visual_id}/visual.json")

    def remove_visual(self, page: PageDoc, visual_id: str) -> None:
        page.visuals.pop(visual_id, None)
        self._removed_folders.add(f"{page.folder}visuals/{visual_id}/")
        self._dirty.discard(f"{page.folder}visuals/{visual_id}/visual.json")

    def remove_page(self, page: PageDoc | str) -> None:
        """Delete a page (by object, id or display name) and everything in its folder.

        pageOrder and activePageName are fixed up; the active page falls back to the first
        remaining page. The last page cannot be removed: a report needs one.
        """
        target = page if isinstance(page, PageDoc) else self.page(page)
        if target.name not in self.pages:
            raise DocumentError(f"There is no page named {target.name!r}")
        if len(self.pages) == 1:
            raise DocumentError("A report needs at least one page")
        self.pages.pop(target.name, None)
        self._removed_folders.add(target.folder)
        self._dirty = {d for d in self._dirty if not d.startswith(target.folder)}
        order = self.pages_meta.get("pageOrder", [])
        self.pages_meta["pageOrder"] = [p for p in order if p != target.name]
        if self.pages_meta.get("activePageName") == target.name:
            self.pages_meta["activePageName"] = next(iter(self.pages))
        self.touch_pages()

    def write_resource(self, package_name: str, path: str, value: Any) -> str:
        part = f"{_RESOURCES}{package_name}/{path}"
        self.package.write_json(part, value)
        self._removed_parts.discard(part)
        return part

    # --- resources: images and themes under Report/StaticResources ---------------------------

    @staticmethod
    def resource_part(package_name: str, path: str) -> str:
        """The package part that holds a resource item's file."""
        return f"{_RESOURCES}{package_name}/{path}"

    @staticmethod
    def split_resource_part(part: str) -> tuple[str, str] | None:
        """(package name, item path) of a part under StaticResources, or None."""
        if not part.startswith(_RESOURCES):
            return None
        package_name, _, path = part[len(_RESOURCES) :].partition("/")
        return (package_name, path) if package_name and path else None

    def write_resource_bytes(self, package_name: str, path: str, data: bytes) -> str:
        """A resource file as it is (an image), unlike ``write_resource`` which writes JSON."""
        part = self.resource_part(package_name, path)
        self.package.write_part(part, data)
        # An item removed earlier in the same batch and written again (Studio names an image by
        # its bytes, so the same picture gets the same name) must survive the commit.
        self._removed_parts.discard(part)
        return part

    def resource_items(self, package_name: str) -> list[dict[str, Any]]:
        """The items report.json registers in a resource package (live dicts), or []."""
        for package in self.report.get("resourcePackages", []):
            if isinstance(package, dict) and package.get("type") == package_name:
                items = package.get("items")
                return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        return []

    def register_resource(self, package_name: str, name: str, path: str, item_type: str) -> None:
        """Add (or replace) an item in report.json's ``resourcePackages``, as Desktop lists it."""
        packages = self.report.setdefault("resourcePackages", [])
        package = next(
            (p for p in packages if isinstance(p, dict) and p.get("type") == package_name), None
        )
        if package is None:
            package = {"name": package_name, "type": package_name, "items": []}
            packages.append(package)
        items = [i for i in package.get("items", []) if i.get("name") != name]
        items.append({"name": name, "path": path, "type": item_type})
        package["items"] = items
        self._removed_parts.discard(self.resource_part(package_name, path))
        self.touch_report()

    def unregister_resource(self, package_name: str, name: str) -> str | None:
        """Drop an item from report.json and delete its file on commit; returns its path.

        A Desktop save garbage-collects unreferenced resources anyway, but a Studio revision
        that keeps a removed image bloats every later copy of the file.
        """
        for package in self.report.get("resourcePackages", []):
            if not isinstance(package, dict) or package.get("type") != package_name:
                continue
            items = package.get("items", [])
            found = next((i for i in items if isinstance(i, dict) and i.get("name") == name), None)
            if found is None:
                return None
            package["items"] = [i for i in items if i is not found]
            self.touch_report()
            path = found.get("path")
            if isinstance(path, str):
                part = self.resource_part(package_name, path)
                if self.package.has(part):
                    self._removed_parts.add(part)
                return path
        return None

    def resource_uses(self, package_name: str, item_name: str) -> list[str]:
        """Parts (page, visual, bookmark, report) that point at a resource item.

        An image is referenced by ``{"ResourcePackageItem": {"PackageName", "ItemName"}}``
        wherever it is used: a page background, an image visual, a button icon.
        """
        found: list[str] = []
        for part, data, _ in self.iter_json_parts():
            if _uses_resource(data, package_name, item_name):
                found.append(part)
        return found

    # --- bookmarks and report extensions ---------------------------------------------------

    def _bookmark_key(self, name: str) -> str:
        """A bookmark's id from its id or, failing that, a unique display name."""
        bookmarks = self.bookmarks()
        if name in bookmarks:
            return name
        matches = [
            key
            for key, value in bookmarks.items()
            if str(value.get("displayName", "")).casefold() == name.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        raise DocumentError(f"There is no bookmark named {name!r}")

    def bookmarks(self) -> dict[str, dict[str, Any]]:
        """Every bookmark, keyed by its id (the file name without ``.bookmark.json``).

        Read on first use: most edits never touch bookmarks. The values are the live
        documents, so change one in place and call ``touch_bookmark``.
        """
        if self._bookmarks is None:
            parts = self.package.read_parts(self._bookmark_parts.values())
            self._bookmarks = {
                key: decode_json(parts[part]) for key, part in self._bookmark_parts.items()
            }
        return dict(self._bookmarks)

    def bookmark(self, name: str) -> dict[str, Any]:
        """A bookmark by id or display name (case-insensitive)."""
        return self.bookmarks()[self._bookmark_key(name)]

    def touch_bookmark(self, name: str) -> None:
        self._dirty.add(self._bookmark_parts[self._bookmark_key(name)])

    def add_bookmark(self, key: str, data: dict[str, Any], *, group: str | None = None) -> str:
        """Add a bookmark file and list it last in the bookmark pane (or in the group whose
        id is ``group``); bookmarks.json is made, with its schema, when the report has none.
        Returns the part name. The bookmark is walked, indexed and written like the others."""
        if not _BOOKMARK_KEY.fullmatch(key):
            raise DocumentError(f"{key!r} cannot name a bookmark file")
        if key in self.bookmarks():
            raise DocumentError(f"There is already a bookmark {key!r}")
        part = f"{BOOKMARKS}{key}.bookmark.json"
        items = self.bookmark_items()
        if group is None:
            items.append({"name": key})
        else:
            found = next(
                (
                    i
                    for i in items
                    if isinstance(i, dict)
                    and i.get("name") == group
                    and isinstance(i.get("children"), list)
                ),
                None,
            )
            if found is None:
                raise DocumentError(f"There is no bookmark group {group!r}")
            found["children"].append(key)
        self._bookmark_parts[key] = part
        assert self._bookmarks is not None  # loaded by bookmarks() above
        self._bookmarks[key] = data
        self._removed_parts.discard(part)
        self._dirty.update({part, BOOKMARKS_META})
        return part

    def bookmark_items(self) -> list[Any]:
        """The live ``items`` list of bookmarks.json (bookmarks, and groups with ``children``),
        made with Desktop's schema when the report has no list yet. Change it in place and
        ``mark_dirty(BOOKMARKS_META)``."""
        meta = self.bookmarks_metadata
        if not meta:
            meta["$schema"] = BOOKMARKS_METADATA_SCHEMA
        if not isinstance(meta.get("items"), list):
            meta["items"] = []
        items: list[Any] = meta["items"]
        return items

    def unlist_bookmark(self, key: str) -> Unlisted:
        """Take a bookmark out of bookmarks.json (its file stays). A group left empty goes
        too, as Desktop drops empty groups, and every bookmark navigator that listed such a
        group lists every bookmark again (``release_bookmark_groups``)."""
        meta = self.bookmarks_metadata
        items = meta.get("items")
        if not isinstance(items, list):
            return Unlisted((), ())
        kept: list[Any] = []
        emptied: list[dict[str, Any]] = []
        changed = False
        for item in items:
            if not isinstance(item, dict):
                kept.append(item)
                continue
            if item.get("name") == key and not isinstance(item.get("children"), list):
                changed = True
                continue
            children = item.get("children")
            if isinstance(children, list) and key in children:
                changed = True
                item["children"] = [c for c in children if c != key]
                if not item["children"]:
                    emptied.append(item)
                    continue
            kept.append(item)
        if not changed:
            return Unlisted((), ())
        meta["items"] = kept
        self._dirty.add(BOOKMARKS_META)
        navigators = self.release_bookmark_groups([str(g.get("name")) for g in emptied])
        names = tuple(str(g.get("displayName") or g.get("name")) for g in emptied)
        return Unlisted(names, tuple(navigators))

    def release_bookmark_groups(self, group_ids: Collection[str]) -> list[tuple[str, str]]:
        """Point every bookmark navigator that lists one of these groups at every bookmark
        (``bookmarkGroup`` "", the pane's "All"): a navigator on a group that no longer exists
        would dangle. Returns (page, visual) of each navigator changed."""
        wanted = set(group_ids)
        changed: list[tuple[str, str]] = []
        if not wanted:
            return changed
        for page in self.pages.values():
            for visual_id, visual in page.visuals.items():
                body = visual.get("visual")
                if not isinstance(body, dict) or body.get("visualType") != "bookmarkNavigator":
                    continue
                objects = body.get("objects")
                entries = objects.get("bookmarks") if isinstance(objects, dict) else None
                for entry in entries if isinstance(entries, list) else []:
                    props = entry.get("properties") if isinstance(entry, dict) else None
                    if isinstance(props, dict) and decode(props.get("bookmarkGroup")) in wanted:
                        props["bookmarkGroup"] = {"expr": {"Literal": {"Value": "''"}}}
                        if (page.name, visual_id) not in changed:
                            changed.append((page.name, visual_id))
                        self.touch_visual(page, visual_id)
        return changed

    def remove_bookmark(self, name: str) -> Unlisted:
        """Delete a bookmark's file and its entry in the list (see ``unlist_bookmark`` for a
        group it empties); returns what the list lost besides the bookmark."""
        key = self._bookmark_key(name)
        part = self._bookmark_parts.pop(key)
        if self._bookmarks is not None:
            self._bookmarks.pop(key, None)
        self._dirty.discard(part)
        self._removed_parts.add(part)
        return self.unlist_bookmark(key)

    @property
    def bookmarks_metadata(self) -> dict[str, Any]:
        """bookmarks/bookmarks.json (the order and groups of the bookmark pane), or {}."""
        if self._bookmarks_meta is None:
            self._bookmarks_meta = (
                self.package.read_json(BOOKMARKS_META) if self.package.has(BOOKMARKS_META) else {}
            )
        return self._bookmarks_meta

    @property
    def extensions(self) -> dict[str, Any]:
        """reportExtensions.json: the report-level measures of a thin report, or {}.

        Read only: Studio does not edit them yet.
        """
        if self._extensions is None:
            self._extensions = (
                self.package.read_json(EXTENSIONS) if self.package.has(EXTENSIONS) else {}
            )
        return self._extensions

    # --- walking every JSON part -------------------------------------------------------------

    def _documents(self, bookmarks: bool) -> Iterator[tuple[str, dict[str, Any], PartKind]]:
        yield REPORT, self.report, "report"
        if self.package.has(PAGES) or self.pages_meta.get("pageOrder"):
            yield PAGES, self.pages_meta, "pages"
        for page in self.pages.values():
            yield page.folder + "page.json", page.data, "page"
            for visual_id, visual in page.visuals.items():
                yield f"{page.folder}visuals/{visual_id}/visual.json", visual, "visual"
        if bookmarks:
            for key, bookmark in self.bookmarks().items():
                yield self._bookmark_parts[key], bookmark, "bookmark"

    def iter_json_parts(self) -> Iterator[tuple[str, dict[str, Any], PartKind]]:
        """(part name, live document, kind) for report, pages list, pages, visuals, bookmarks.

        Reference scanners walk this without knowing the folder layout, edit a document in
        place and call ``mark_dirty`` with its part name.
        """
        yield from self._documents(bookmarks=True)

    def mark_dirty(self, part_name: str) -> None:
        """Flag a part edited in place so the next ``commit`` writes it. bookmarks.json may be
        marked before the report has one (the first bookmark makes it)."""
        if part_name == BOOKMARKS_META:
            _ = self.bookmarks_metadata  # load it so commit has the document to write
        elif not any(name == part_name for name, _, _ in self.iter_json_parts()):
            raise DocumentError(f"There is no editable report part {part_name!r}")
        self._dirty.add(part_name)

    def _bookmark_list_is_empty(self) -> bool:
        """bookmarks.json lists nothing and no bookmark file is left."""
        items = (self._bookmarks_meta or {}).get("items")
        return not items and not self.bookmarks()

    def commit(self) -> list[str]:
        """Write every changed part into the package; returns the parts written."""
        for folder in self._removed_folders:
            for name in list(self.package.names()):
                if name.startswith(folder):
                    self.package.remove_part(name)
        for part in self._removed_parts:
            self.package.remove_part(part)
        # Bookmarks are read on demand; if none were loaded none can be dirty.
        values = {
            name: value for name, value, _ in self._documents(bookmarks=self._bookmarks is not None)
        }
        # An empty list the report never had is not written: bookmarks.json without its
        # schema is not a file Desktop reads.
        if self._bookmarks_meta or (
            self._bookmarks_meta is not None and self.package.has(BOOKMARKS_META)
        ):
            values[BOOKMARKS_META] = self._bookmarks_meta
        # Once the last bookmark is gone the list goes too, as Desktop 2.157 removes an empty
        # bookmarks.json when it saves.
        if BOOKMARKS_META in self._dirty and self._bookmark_list_is_empty():
            values.pop(BOOKMARKS_META, None)
            self.package.remove_part(BOOKMARKS_META)
        written = []
        for part in sorted(self._dirty):
            if part not in values:
                continue
            self.package.write_json(part, values[part])
            written.append(part)
        self._dirty.clear()
        self._removed_folders.clear()
        self._removed_parts.clear()
        return written
