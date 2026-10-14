"""The theme an imported report is drawn with: base theme merged with the custom theme.

Desktop reads two themes: the base theme (``themeCollection.baseTheme``) and the custom
theme a user imported or Studio wrote (``themeCollection.customTheme``). The custom one is
merged over the base one: dictionaries merge key by key, so a custom theme that sets one
``textClasses`` entry keeps the base theme's other entries, while lists (``dataColors``) and
plain values are replaced. Either theme can live in RegisteredResources or SharedResources
(a gallery theme Desktop copied in is stored under SharedResources even as the custom one).

ThemeDataColor ids do not index the theme's background and foreground, as an earlier design
assumed. Measured in Desktop 2.157 with a theme whose colors were all distinct: ColorId 0 is
always white, 1 always black, 2..9 are ``dataColors[0..7]`` and 10 and up are invalid (the
default fill is drawn), so ``theme_palette`` is the one place that says so.
"""

from __future__ import annotations

import base64
import copy
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from functools import lru_cache
from typing import Any

Json = dict[str, Any]

_ROOT = "Report/StaticResources/"
_MAX_THEME_BYTES = 2_000_000
_MAX_IMAGE_BYTES = 4_000_000
_HEX6 = re.compile(r"#[0-9a-fA-F]{6}")

# The Power BI default theme, used when the report names no theme Studio can read. Values are
# those of the CY24SU02 base theme Desktop writes into new reports.
DEFAULT_THEME: Json = {
    "name": "Default",
    "dataColors": [
        "#118DFF",
        "#12239E",
        "#E66C37",
        "#6B007B",
        "#E044A7",
        "#744EC2",
        "#D9B300",
        "#D64550",
    ],
    "foreground": "#252423",
    "foregroundNeutralSecondary": "#605E5C",
    "foregroundNeutralTertiary": "#B3B0AD",
    "background": "#FFFFFF",
    "backgroundLight": "#F3F2F1",
    "backgroundNeutral": "#C8C6C4",
    "tableAccent": "#118DFF",
    "textClasses": {
        "callout": {"fontSize": 45, "fontFace": "DIN", "color": "#252423"},
        "title": {"fontSize": 12, "fontFace": "DIN", "color": "#252423"},
        "header": {"fontSize": 12, "fontFace": "Segoe UI Semibold", "color": "#252423"},
        "label": {"fontSize": 10, "fontFace": "Segoe UI", "color": "#252423"},
    },
    "visualStyles": {},
}


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Json:
    """``override`` over ``base``: dicts merge per key, lists and scalars are replaced."""
    merged: Json = dict(base)
    for key, item in override.items():
        current = merged.get(key)
        if isinstance(item, Mapping) and isinstance(current, Mapping):
            merged[key] = deep_merge(current, item)
        else:
            merged[key] = copy.deepcopy(item)
    return merged


def theme_palette(data_colors: Sequence[Any]) -> list[str]:
    """ColorId order: white, black, then the first eight data colors.

    A data color that is not a hex stays as an empty slot so later ids keep their meaning;
    a slot with no color makes the id invalid, which draws the default fill.
    """
    palette = ["#ffffff", "#000000"]
    for item in list(data_colors)[:8]:
        palette.append(item.lower() if isinstance(item, str) and _HEX6.fullmatch(item) else "")
    return palette


def _read_json(archive: zipfile.ZipFile, part: str) -> Json | None:
    try:
        info = archive.getinfo(part)
        if info.file_size > _MAX_THEME_BYTES:
            return None
        raw = archive.read(info)
        found = json.loads(raw.decode("utf-8-sig"))
    except (KeyError, ValueError, OSError, RuntimeError, zipfile.BadZipFile):
        return None
    return found if isinstance(found, dict) else None


def _themes(archive: zipfile.ZipFile, report: Json, key: str) -> Json | None:
    """The theme a themeCollection entry names, wherever its file is stored."""
    collection = report.get("themeCollection")
    entry = collection.get(key) if isinstance(collection, dict) else None
    packages = report.get("resourcePackages")
    if not isinstance(entry, dict) or not entry.get("name") or not isinstance(packages, list):
        return None
    preferred = entry.get("type", "SharedResources")
    for package in sorted(
        (p for p in packages if isinstance(p, dict)), key=lambda p: p.get("type") != preferred
    ):
        items = package.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("name") != entry["name"]:
                continue
            if not isinstance(item.get("path"), str):
                continue
            found = _read_json(
                archive, f"{_ROOT}{package.get('name', package.get('type'))}/{item['path']}"
            )
            if found is not None:
                return found
    return None


def _legacy_base(archive: zipfile.ZipFile, report: Json) -> Json | None:
    # Reports Studio wrote before themeCollection was read carry the theme only as a
    # resource item of type BaseTheme.
    packages = report.get("resourcePackages")
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict):
            continue
        items = package.get("items")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("type") == "BaseTheme":
                found = _read_json(archive, f"{_ROOT}{package.get('name')}/{item.get('path')}")
                if found is not None:
                    return found
    return None


def read_report_config(archive: zipfile.ZipFile) -> Json:
    """report.json as a dict, or {} for a report without one."""
    found = _read_json(archive, "Report/definition/report.json")
    return found if found is not None else {}


@lru_cache(maxsize=8)
def _cached_theme(archive_path: str, blob: str) -> str:
    # Keyed by the file's hash, not the data model: any revision can change the theme.
    with zipfile.ZipFile(archive_path) as z:
        report = read_report_config(z)
        base = _themes(z, report, "baseTheme") or _legacy_base(z, report)
        custom = _themes(z, report, "customTheme")
    merged = deep_merge(DEFAULT_THEME, base) if base else copy.deepcopy(DEFAULT_THEME)
    if custom:
        merged = deep_merge(merged, custom)
    return json.dumps(merged)


def merged_theme(archive_path: str, blob: str) -> Json:
    """The effective theme of the report file ``archive_path`` (whose sha256 is ``blob``).

    A copy: callers may change it. Themes that cannot be read fall back to the Power BI
    default theme, as Desktop does.
    """
    loaded = json.loads(_cached_theme(archive_path, blob))
    assert isinstance(loaded, dict)
    return loaded


def _hex_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and _HEX6.fullmatch(value) else None


_FONT_PART = re.compile(r"""'([^']+)'|"([^"]+)"|([^,]+)""")
# Letters and digits of any script (a CJK family is a real font name); no quote, brace,
# semicolon or parenthesis ever gets through, so a name cannot end the CSS value.
_FONT_NAME = re.compile(r"[^\W_][\w .-]{0,59}")
_BARE_FONT = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_GENERIC_FONTS = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui"}


def css_font_family(text: Any) -> str | None:
    """A font-family value made only of validated names, or None.

    Desktop stores stacks like ``'Segoe UI Bold', wf_segoe-ui_bold, helvetica, arial,
    sans-serif``; only plain family names survive, so the result is never markup.
    """
    if not isinstance(text, str) or len(text) > 400:
        return None
    names: list[str] = []
    for quoted, double, bare in _FONT_PART.findall(text):
        name = (quoted or double or bare).strip().strip("'\"").strip()
        if not _FONT_NAME.fullmatch(name):
            continue
        # Quoted unless it is a plain identifier: a name with a space, a leading digit or
        # non-ASCII letters is safest as a string.
        names.append(name if name in _GENERIC_FONTS or _BARE_FONT.fullmatch(name) else f'"{name}"')
        if len(names) == 6:
            break
    return ", ".join(names) or None


def _class(theme: Mapping[str, Any], name: str) -> Json:
    classes = theme.get("textClasses")
    found = classes.get(name) if isinstance(classes, dict) else None
    return found if isinstance(found, dict) else {}


def text_class(theme: Mapping[str, Any], name: str) -> Json:
    """One of the theme's text classes (callout, title, header, label) as
    ``{fontSize (pt), family (CSS), color}`` with the default theme's values as fallback."""
    own = _class(theme, name)
    default = _class(DEFAULT_THEME, name)
    size = own.get("fontSize", default.get("fontSize", 10))
    return {
        "fontSize": float(size)
        if isinstance(size, int | float) and not isinstance(size, bool)
        else 10.0,
        "family": css_font_family(own.get("fontFace")) or css_font_family(default.get("fontFace")),
        "color": _hex_or_none(own.get("color")) or _hex_or_none(theme.get("foreground")),
    }


def theme_payload(theme: Mapping[str, Any]) -> Json:
    """The theme as the client receives it: colors, text classes and the ColorId palette.

    ``visualStyles`` stays server side; the tokens of every visual already include what it
    holds.
    """
    colors = [c for c in theme.get("dataColors", []) if _hex_or_none(c)] or list(
        DEFAULT_THEME["dataColors"]
    )
    payload: Json = {
        "name": theme.get("name") if isinstance(theme.get("name"), str) else None,
        "dataColors": colors,
        "palette": theme_palette(theme.get("dataColors", [])),
    }
    for key in (
        "foreground",
        "background",
        "foregroundNeutralSecondary",
        "foregroundNeutralTertiary",
        "backgroundLight",
        "backgroundNeutral",
        "tableAccent",
        "good",
        "neutral",
        "bad",
        "hyperlink",
    ):
        found = _hex_or_none(theme.get(key)) or _hex_or_none(DEFAULT_THEME.get(key))
        if found:
            payload[key] = found
    payload["textClasses"] = {}
    for name in ("callout", "title", "header", "label"):
        cls = text_class(theme, name)
        payload["textClasses"][name] = {
            "fontSize": cls["fontSize"],
            "fontSizePx": round(cls["fontSize"] * 4 / 3, 2),
            "fontFamily": cls["family"],
            "color": cls["color"],
        }
    return payload


def image_data_uri(
    archive: zipfile.ZipFile, report: Mapping[str, Any], package_name: Any, item_name: Any
) -> tuple[str | None, str | None]:
    """(data URI, MIME type) of a RegisteredResources/SharedResources image, or (None, None).

    Only PNG, JPEG and GIF bytes (by signature, never by name) are passed through; SVG is
    left to the caller's sanitizer. Anything larger than 4 MB is dropped.
    """
    packages = report.get("resourcePackages")
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict) or package.get("name") != package_name:
            continue
        items = package.get("items")
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or item.get("name") != item_name:
                continue
            if item.get("type") != "Image" or not isinstance(item.get("path"), str):
                continue
            part = f"{_ROOT}{package['name']}/{item['path']}"
            try:
                info = archive.getinfo(part)
                if info.file_size > _MAX_IMAGE_BYTES:
                    return None, None
                data = archive.read(info)
            except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
                return None, None
            mime = _sniff(data)
            if mime is None:
                return None, None
            return f"data:{mime};base64," + base64.b64encode(data).decode(), mime
    return None, None


def literal_image_uri(uri: str) -> tuple[str | None, str | None]:
    """(data URI, MIME type) of a base64 ``data:image/...`` literal, or (None, None).

    The bytes are checked by signature exactly as a stored resource is, whatever type the
    URI claims, and the URI is rebuilt from them; anything larger than 4 MB is dropped.
    """
    header, _, body = uri.partition(",")
    if not header.lower().startswith("data:image/") or not header.lower().endswith(";base64"):
        return None, None
    if len(body) > _MAX_IMAGE_BYTES * 4 // 3 + 8:
        return None, None
    try:
        data = base64.b64decode(body, validate=True)
    except ValueError:
        return None, None
    mime = _sniff(data)
    if mime is None:
        return None, None
    return f"data:{mime};base64," + base64.b64encode(data).decode(), mime


def _sniff(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    head = data[:512].lstrip().lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in data[:2048].lower():
        return "image/svg+xml"
    return None
