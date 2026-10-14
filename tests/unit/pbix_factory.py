"""Small PBIX builders shared by the package, document, diff and fixture tests.

Not a test module. Each builder writes what Desktop writes: a BOM-prefixed content-type
list first, a STORED DataModel last, SecurityBindings before it, compact UTF-8 PBIR JSON.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SCHEMA_ROOT = "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
STAMP = (2026, 1, 2, 3, 4, 6)
DEFINITION = "Report/definition/"


def schema(kind: str, version: str) -> str:
    return f"{SCHEMA_ROOT}{kind}/{version}/schema.json"


def dump(
    value: Any,
    *,
    bom: bool = False,
    utf16: bool = False,
    indent: int | None = None,
    sort_keys: bool = False,
) -> bytes:
    separators = None if indent is not None else (",", ":")
    text = json.dumps(
        value, ensure_ascii=False, separators=separators, indent=indent, sort_keys=sort_keys
    )
    if utf16:
        return text.encode("utf-16-le")
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def content_types(
    defaults: Iterable[str] = ("png", "json", "dax"),
    overrides: Iterable[str] = ("Version", "DataModel", "SecurityBindings"),
) -> bytes:
    body = "".join(f'<Default Extension="{e}" ContentType="" />' for e in defaults)
    body += "".join(f'<Override PartName="/{p}" ContentType="" />' for p in overrides)
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">{body}</Types>'
    )
    return b"\xef\xbb\xbf" + xml.encode("utf-8")


def make_pbix(
    path: Path,
    parts: Mapping[str, bytes] | Iterable[tuple[str, bytes]],
    *,
    data_model: bytes | None = b"\x00DATAMODEL" * 50,
    security: bool = True,
    types: bytes | None = None,
) -> Path:
    """Write a PBIX: content types, the given parts in order, SecurityBindings, DataModel."""
    items = list(parts.items() if isinstance(parts, Mapping) else parts)
    with zipfile.ZipFile(path, "w") as archive:
        info = zipfile.ZipInfo("[Content_Types].xml", date_time=STAMP)
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, types if types is not None else content_types())
        for name, data in items:
            info = zipfile.ZipInfo(name, date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        if security:
            info = zipfile.ZipInfo("SecurityBindings", date_time=STAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, b"\x01signed-by-dpapi" * 8)
        if data_model is not None:
            info = zipfile.ZipInfo("DataModel", date_time=STAMP)
            info.compress_type = zipfile.ZIP_STORED
            archive.writestr(info, data_model)
    return path


def visual(
    name: str,
    title: str | None = None,
    *,
    kind: str = "card",
    x: float = 0,
    y: float = 0,
    width: float = 100,
    height: float = 50,
    z: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    """A visual.json with a literal title (when given) and one measure projection."""
    body: dict[str, Any] = {
        "visualType": kind,
        "query": {
            "queryState": {
                "Values": {
                    "projections": [
                        {
                            "field": {
                                "Measure": {
                                    "Expression": {"SourceRef": {"Entity": "M"}},
                                    "Property": "Total",
                                }
                            },
                            "queryRef": "M.Total",
                            "nativeQueryRef": "Total",
                        }
                    ]
                }
            }
        },
    }
    if title is not None:
        body["visualContainerObjects"] = {
            "title": [{"properties": {"text": {"expr": {"Literal": {"Value": f"'{title}'"}}}}}]
        }
    document: dict[str, Any] = {
        "$schema": schema("visualContainer", "2.12.0"),
        "name": name,
        "position": {"x": x, "y": y, "z": z, "height": height, "width": width, "tabOrder": z},
        "visual": body,
    }
    document.update(extra)
    return document


def report_parts(
    pages: list[tuple[str, str, list[dict[str, Any]]]],
    *,
    report: dict[str, Any] | None = None,
    resources: Mapping[str, bytes] | None = None,
    active: str | None = None,
) -> dict[str, bytes]:
    """The Report/definition parts of a report; ``pages`` is (id, display name, visuals)."""
    parts: dict[str, bytes] = {
        DEFINITION + "version.json": dump(
            {"$schema": schema("versionMetadata", "1.0.0"), "version": "2.0.0"}
        ),
        DEFINITION + "report.json": dump(
            report
            or {
                "$schema": schema("report", "3.3.0"),
                "themeCollection": {},
                "resourcePackages": [],
            }
        ),
        DEFINITION + "pages/pages.json": dump(
            {
                "$schema": schema("pagesMetadata", "1.1.0"),
                "pageOrder": [p[0] for p in pages],
                "activePageName": active or pages[0][0],
            }
        ),
    }
    for page_id, display, visuals in pages:
        parts[f"{DEFINITION}pages/{page_id}/page.json"] = dump(
            {
                "$schema": schema("page", "2.1.0"),
                "name": page_id,
                "displayName": display,
                "displayOption": "FitToPage",
                "height": 720,
                "width": 1280,
            }
        )
        for item in visuals:
            parts[f"{DEFINITION}pages/{page_id}/visuals/{item['name']}/visual.json"] = dump(item)
    for name, data in (resources or {}).items():
        parts[name] = data
    return parts
