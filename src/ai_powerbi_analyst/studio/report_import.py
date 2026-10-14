"""Import original PBIX report definitions and offline data without rewriting the report."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import psutil
from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel
from ai_powerbi_analyst.integrity import sha256_file

from . import model_facts, pbir_expr
from .contracts import (
    AdvancedDaxProposal,
    DatasetSnapshot,
    RelationshipProposal,
    SourceInventoryItem,
)
from .ingestion import StudioIngestionService
from .report_captions import DYNAMIC_TITLE, visible_caption


class ImportReportRequest(ClosedModel):
    path: str = Field(min_length=1, max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=200)


class ImportedVisual(ClosedModel):
    id: str
    title: str
    visual_type: str
    x: float = 0
    y: float = 0
    width: float = 300
    height: float = 200
    bindings: list[dict[str, Any]] = Field(default_factory=list)
    definition: dict[str, Any] = Field(default_factory=dict)
    # Everything below has a default so reports stored before editing existed still load;
    # report_pages re-reads such a report from its archive.
    # Stacking rank: a Desktop save rewrites member order alphabetically by folder name,
    # so the order visuals are listed in says nothing. Draw by z.
    z: int = 0
    tab_order: int | None = None
    # The visual's own isHidden flag; a hidden group also hides its members
    # (is_effectively_hidden).
    hidden: bool = False
    parent_group: str | None = None
    # The text a user sees on the visual (report_captions), unlike ``title``, which is the
    # label Studio lists it by and falls back to a field name.
    caption: str | None = None
    # Up to 8 hexes the visual paints with (literal fills and font colors, theme colors
    # resolved), in the order that says most about its look: data colors first.
    colors: list[str] = Field(default_factory=list)
    group_children: list[str] = Field(default_factory=list)
    container_kind: Literal["visual", "group"] = "visual"


class ImportedPage(ClosedModel):
    id: str
    title: str
    width: float = 1280
    height: float = 720
    visuals: list[ImportedVisual] = Field(default_factory=list)
    hidden: bool = False
    page_type: Literal["default", "drillthrough", "tooltip"] = "default"
    order: int = 0


class ImportedReport(ClosedModel):
    id: str
    workspace_id: str
    name: str
    source_path: str
    archived_path: str
    source_hash: str
    base_hash: str
    format: str
    pages: list[ImportedPage]
    tables: list[dict[str, Any]] = Field(default_factory=list)
    measures: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    snapshot_id: str | None = None
    warnings: list[str] = Field(default_factory=list)
    # Stored reports without it came from the extractor that lost calculated-table joins.
    relationships_version: int = 0
    # The head revision once Studio edited the report: a whole PBIX (a Desktop save
    # rewrites the whole file too) and its sha256. None while the report is unedited.
    revision_path: str | None = None
    revision_hash: str | None = None
    # Stored reports without it hold pages read before z, hidden flags, captions and colors
    # existed; report_pages reads them again from the head file.
    pages_version: int = 0
    # The head model's own facts (pbix_extract.read_model_facts, bounded by
    # model_facts.stored): tables, columns, relationships, hierarchies, roles, calculation
    # groups, parameters, partitions, settings. Read at import and whenever the head's model
    # changes; None while unknown (a report imported before they were read, a failed read, a
    # report without a model). Legacy readers may work from tables/measures/relationships
    # alone; a current read without facts leaves local numeric evaluation unavailable.
    model: dict[str, Any] | None = None
    # model_facts.digest of the model the snapshot's rows were decoded from: which calculated
    # columns, names and refreshes the rows reflect. None when unknown.
    snapshot_model: dict[str, Any] | None = None
    # 0 identifies legacy imports whose model facts were never required. A current import
    # or facts read (including a failed read) sets 1: unknown metadata must not silently
    # return to the legacy snapshot evaluator. Persisted independently of successful facts.
    model_read_version: int = Field(default=0, ge=0)


def report_archive(report: ImportedReport) -> tuple[Path, str]:
    """(the PBIX that holds the report as it is now, the sha256 it must have).

    original.pbix stays immutable; edits write whole-file revisions beside it. Every
    reader that draws the report reads the head revision, so an edit shows on the canvas
    without touching the original. Callers verify the hash before trusting the file.
    """
    if (report.revision_path is None) != (report.revision_hash is None):
        raise ValueError("The report's current revision is incomplete; reopen the report")
    if report.revision_path is not None and report.revision_hash is not None:
        return Path(report.revision_path), report.revision_hash
    return Path(report.archived_path), report.source_hash


# 2: relationships come from the model metadata, calculated-table ends included. Version
# 1 imports lost FactSales[DateKey] -> Date[Date] (Date is a CALENDAR table), and chat
# then said Date had no relationships.
RELATIONSHIPS_VERSION = 2
# 2: pages carry z, tab order, hidden flags, groups, captions, colors and page types.
PAGES_VERSION = 2


def _json(data: bytes) -> dict[str, Any]:
    value = json.loads(data.decode("utf-16-le" if b"\x00" in data[:16] else "utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Report definition must be a JSON object")
    return value


def _literal(value: object) -> str | None:
    if isinstance(value, dict):
        literal = value.get("Literal")
        if isinstance(literal, dict) and isinstance(literal.get("Value"), str):
            text: str = literal["Value"]
            # A quoted string doubles its quotes ('Agent''s Sales'); strip("'") alone left
            # the doubled pair in the name.
            if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
                return text[1:-1].replace("''", "'")
            return text.strip("'")
        for child in value.values():
            result = _literal(child)
            if result:
                return result
    return None


def _bindings(value: object, role: str = "") -> list[dict[str, Any]]:
    found = []
    if isinstance(value, dict):
        for kind in ("Measure", "Column"):
            field = value.get(kind)
            if isinstance(field, dict):
                ref = field.get("Expression", {}).get("SourceRef", {})
                found.append(
                    {
                        "kind": kind.lower(),
                        "table": ref.get("Entity", ref.get("Source", "")),
                        "name": field.get("Property", ""),
                        "role": role,
                    }
                )
                return found
        for key, child in value.items():
            found.extend(
                _bindings(
                    child,
                    key
                    if key in {"Category", "Y", "X", "Values", "Data", "Series", "Rows", "Columns"}
                    else role,
                )
            )
    elif isinstance(value, list):
        for child in value:
            found.extend(_bindings(child, role))
    return list({json.dumps(item, sort_keys=True): item for item in found}.values())


# ThemeDataColor ids 0 and 1 are always white and black, and 2..9 are dataColors[0..7]
# (measured in Desktop 2.157: not the theme's background and foreground; ids from 10 up
# are invalid and paint the default fill).
_FIXED_THEME_COLORS = ("#FFFFFF", "#000000")
_MAX_COLORS = 8
_MAX_THEME_BYTES = 1024**2


def _solid_fills(node: object, found: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        if "solid" in node:
            found.append(node)
            return
        for child in node.values():
            _solid_fills(child, found)
    elif isinstance(node, list):
        for child in node:
            _solid_fills(child, found)


def _visual_colors(raw: dict[str, Any], theme: list[str]) -> list[str]:
    """The hexes a visual paints with: data-point fills first, then every other literal or
    theme color of its formatting. Colors chosen by a measure or a rule are not knowable
    here and are left out."""
    body = raw.get("visual", {})
    if not isinstance(body, dict):
        return []
    palette = [*_FIXED_THEME_COLORS, *theme[:_MAX_COLORS]]
    sources: list[tuple[int, object]] = []
    for holder_key in ("objects", "visualContainerObjects", "vcObjects"):
        holder = body.get(holder_key)
        if not isinstance(holder, dict):
            continue
        for name, entries in holder.items():
            sources.append((0 if name == "dataPoint" else 1, entries))
    colors: list[str] = []
    for _, entries in sorted(sources, key=lambda item: item[0]):
        for entry in entries if isinstance(entries, list) else []:
            props = entry.get("properties") if isinstance(entry, dict) else None
            if not isinstance(props, dict) or pbir_expr.decode(props.get("show")) is False:
                continue
            fills: list[dict[str, Any]] = []
            _solid_fills(props, fills)
            for fill in fills:
                color = _fill_hex(pbir_expr.decode(fill), palette)
                if color and color not in colors:
                    colors.append(color)
    return colors[:_MAX_COLORS]


def _fill_hex(value: object, palette: list[str]) -> str:
    """The hex of a decoded solid fill ({hex} or {theme, percent}), "" when unknown."""
    if not isinstance(value, dict):
        return ""
    try:
        if "hex" in value:
            return pbir_expr.hex_color(value["hex"])
        slot = value.get("theme")
        if isinstance(slot, int) and 0 <= slot < len(palette) and palette[slot]:
            return pbir_expr.shade(palette[slot], float(value.get("percent", 0)))
    except (pbir_expr.ExpressionError, TypeError, ValueError):
        pass
    return ""


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(max(-(10**9), min(float(value), 10**9)))


def _bound(position: dict[str, Any], key: str, default: float) -> float:
    return max(0, min(float(position.get(key, default)), 20000))


def _visual(
    raw: dict[str, Any], fallback_id: str, theme: list[str] | None = None
) -> ImportedVisual:
    visual = raw.get("visual", {})
    group = raw.get("visualGroup")
    is_group = isinstance(group, dict) and "visual" not in raw
    kind = visual.get("visualType", "visualGroup" if "visualGroup" in raw else "unknown")
    container_title = None
    objects = visual.get("visualContainerObjects") or visual.get("vcObjects") or {}
    titles = objects.get("title") if isinstance(objects, dict) else None
    for obj in titles if isinstance(titles, list) else []:
        if isinstance(obj, dict):
            container_title = _literal(obj.get("properties", {}).get("text")) or container_title
    bindings = _bindings(visual.get("query", {}).get("queryState", {}))
    position = raw.get("position", {})
    caption = None if is_group else visible_caption(raw)
    if isinstance(group, dict) and is_group:
        name = group.get("displayName")
        title = name if isinstance(name, str) and name.strip() else kind
    else:
        title = (
            caption
            if caption and caption != DYNAMIC_TITLE
            else container_title or (bindings[0]["name"] if bindings else kind)
        )
    parent = raw.get("parentGroupName")
    return ImportedVisual(
        id=raw.get("name", fallback_id),
        title=title,
        visual_type=kind,
        bindings=bindings,
        definition=raw,
        z=_int_or_none(position.get("z")) or 0,
        tab_order=_int_or_none(position.get("tabOrder")),
        hidden=raw.get("isHidden") is True,
        parent_group=parent if isinstance(parent, str) and parent else None,
        caption=caption,
        colors=[] if is_group else _visual_colors(raw, theme or []),
        container_kind="group" if is_group else "visual",
        x=_bound(position, "x", 0),
        y=_bound(position, "y", 0),
        width=_bound(position, "width", 300),
        height=_bound(position, "height", 200),
    )


def _link_groups(visuals: list[ImportedVisual]) -> list[ImportedVisual]:
    """Fill each group's ``group_children`` (direct members, bottom of the stack first) and
    give every member its page position.

    A member's stored position is relative to its group's origin (measured in Desktop
    2.157: a child stored at 100,100 in a group at 300,300 drew at 400,400), and a group
    inside a group is relative to the outer one. ``x``/``y`` here are page pixels, so the
    canvas and chat need no group arithmetic; ``definition`` keeps the stored values.
    """
    members: dict[str, list[tuple[int, int, str]]] = {}
    for index, visual in enumerate(visuals):
        if visual.parent_group:
            members.setdefault(visual.parent_group, []).append((visual.z, index, visual.id))
    by_id = {v.id: v for v in visuals}

    def origin(visual: ImportedVisual) -> tuple[float, float]:
        x = y = 0.0
        seen = {visual.id}
        parent = by_id.get(visual.parent_group) if visual.parent_group else None
        while parent is not None and parent.id not in seen:
            x, y = x + parent.x, y + parent.y
            seen.add(parent.id)
            parent = by_id.get(parent.parent_group) if parent.parent_group else None
        return x, y

    linked = []
    for visual in visuals:
        update: dict[str, Any] = {}
        if visual.container_kind == "group" and visual.id in members:
            update["group_children"] = [i for *_, i in sorted(members[visual.id])]
        if visual.parent_group and visual.parent_group in by_id:
            ox, oy = origin(visual)
            update["x"], update["y"] = visual.x + ox, visual.y + oy
        linked.append(visual.model_copy(update=update) if update else visual)
    return linked


def is_effectively_hidden(page: ImportedPage, visual_id: str) -> bool:
    """Whether a visual is not drawn: its own flag, or that of a group it sits in."""
    by_id = {v.id: v for v in page.visuals}
    seen: set[str] = set()
    current = by_id.get(visual_id)
    while current is not None and current.id not in seen:
        if current.hidden:
            return True
        seen.add(current.id)
        current = by_id.get(current.parent_group) if current.parent_group else None
    return False


def _theme_colors(
    archive: zipfile.ZipFile, entries: dict[str, zipfile.ZipInfo], report: dict[str, Any]
) -> list[str]:
    """The report theme's dataColors (the custom theme's when it sets them, else the base
    theme's), each a hex or "" for a value that is not one."""
    collection, packages = report.get("themeCollection"), report.get("resourcePackages")
    if not isinstance(collection, dict) or not isinstance(packages, list):
        return []
    for key in ("customTheme", "baseTheme"):
        entry = collection.get(key)
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        preferred = entry.get("type", "SharedResources")
        for package in sorted(
            (p for p in packages if isinstance(p, dict)), key=lambda p: p.get("type") != preferred
        ):
            for item in package.get("items", []) if isinstance(package.get("items"), list) else []:
                if not isinstance(item, dict) or item.get("name") != entry["name"]:
                    continue
                part = (
                    f"Report/StaticResources/{package.get('name', package.get('type'))}/"
                    f"{item.get('path')}"
                )
                info = entries.get(part)
                if info is None or info.file_size > _MAX_THEME_BYTES:
                    continue
                try:
                    colors = _json(archive.read(info)).get("dataColors")
                except (ValueError, KeyError, OSError, RuntimeError, zipfile.BadZipFile):
                    continue
                if isinstance(colors, list) and colors:
                    return [_hex_or_blank(c) for c in colors]
    return []


def _hex_or_blank(value: object) -> str:
    try:
        return pbir_expr.hex_color(value) if isinstance(value, str) else ""
    except pbir_expr.ExpressionError:
        return ""


def _page_type(page: dict[str, Any]) -> Literal["default", "drillthrough", "tooltip"]:
    binding = page.get("pageBinding")
    for value in (page.get("type"), binding.get("type") if isinstance(binding, dict) else None):
        if isinstance(value, str) and value.casefold() in {"drillthrough", "tooltip"}:
            return "drillthrough" if value.casefold() == "drillthrough" else "tooltip"
    return "default"


def read_report(source: Path) -> tuple[list[ImportedPage], str, bool]:
    """Read bounded JSON members only. No ZIP entry is extracted as a filesystem path."""
    if source.suffix.casefold() != ".pbix" or not zipfile.is_zipfile(source):
        raise ValueError("Choose a valid Power BI report (.pbix)")
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        if len(entries) > 20000 or len({e.filename for e in entries}) != len(entries):
            raise ValueError("The report archive has too many or duplicate components")
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or ":" in path.parts[0]
            ):
                raise ValueError("The report contains an invalid component path")
        names = {entry.filename for entry in entries}
        json_entries = [
            e
            for e in entries
            if e.filename.startswith("Report/definition/")
            and e.filename.endswith(".json")
            or e.filename == "Report/Layout"
        ]
        if sum(e.file_size for e in json_entries) > 32 * 1024**2 or any(
            e.file_size > 8 * 1024**2 for e in json_entries
        ):
            raise ValueError("Report definitions exceed the local import limit")
        definitions = {e.filename: _json(archive.read(e)) for e in json_entries}
        pages = []
        if "Report/definition/pages/pages.json" in definitions:
            order = definitions["Report/definition/pages/pages.json"].get("pageOrder", [])
            paths = [n for n in definitions if n.endswith("/page.json")]
            paths.sort(
                key=lambda n: (
                    order.index(n.split("/")[-2]) if n.split("/")[-2] in order else len(order)
                )
            )
            theme = _theme_colors(
                archive,
                {e.filename: e for e in entries},
                definitions.get("Report/definition/report.json", {}),
            )
            for order_index, page_file in enumerate(paths):
                page = definitions[page_file]
                prefix = page_file.removesuffix("page.json") + "visuals/"
                visuals = _link_groups(
                    [
                        _visual(v, n.split("/")[-2], theme)
                        for n, v in definitions.items()
                        if n.startswith(prefix) and n.endswith("/visual.json")
                    ]
                )
                pages.append(
                    ImportedPage(
                        id=page["name"],
                        title=page.get("displayName", page["name"]),
                        width=page.get("width", 1280),
                        height=page.get("height", 720),
                        visuals=visuals,
                        hidden=page.get("visibility") == "HiddenInViewMode",
                        page_type=_page_type(page),
                        order=order_index,
                    )
                )
            format_name = "PBIR"
        elif "Report/Layout" in definitions:
            layout = definitions["Report/Layout"]
            for index, section in enumerate(layout.get("sections", [])):
                visuals = []
                for vi, container in enumerate(section.get("visualContainers", [])):
                    config = json.loads(container.get("config", "{}"))
                    single = config.get("singleVisual", {})
                    raw = {
                        "name": config.get("name", f"visual-{vi}"),
                        "position": {
                            **{k: container.get(k, 0) for k in ("x", "y", "width", "height")},
                            **{k: container[k] for k in ("z", "tabOrder") if k in container},
                        },
                        "visual": single,
                    }
                    # A legacy visual hides itself through its display mode.
                    display = single.get("display")
                    if isinstance(display, dict) and display.get("mode") == "hidden":
                        raw["isHidden"] = True
                    item = _visual(raw, f"visual-{vi}")
                    bindings = _bindings(single.get("prototypeQuery", {}))
                    aliases = {
                        item.get("Name"): item.get("Entity")
                        for item in single.get("prototypeQuery", {}).get("From", [])
                    }
                    for binding in bindings:
                        binding["table"] = aliases.get(binding["table"], binding["table"])
                    visuals.append(item.model_copy(update={"bindings": bindings}))
                pages.append(
                    ImportedPage(
                        id=section.get("name", f"page-{index}"),
                        title=section.get("displayName", f"Page {index + 1}"),
                        width=section.get("width", 1280),
                        height=section.get("height", 720),
                        visuals=visuals,
                        order=index,
                    )
                )
            format_name = "Legacy PBIX"
        else:
            raise ValueError(
                "This PBIX has no readable report definitions. "
                "Encrypted reports must be opened in Power BI Desktop."
            )
        if not pages or len(pages) > 200 or sum(len(p.visuals) for p in pages) > 4000:
            raise ValueError("Report page or visual count is outside the supported import limits")
        return pages, format_name, "DataModel" in names


def extract_model(source: Path, destination: Path) -> dict[str, Any]:
    """Bound the decoder independently of the API process on lower-memory laptops."""
    available = psutil.virtual_memory().available
    if available < 512 * 1024**2:
        raise ValueError("Free at least 512 MiB of RAM before importing this report")
    budget = min(2 * 1024**3, int(available * 0.55))
    with (destination / "decoder.log").open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 - fixed Python/module argv; no shell
            [
                sys.executable,
                "-m",
                "ai_powerbi_analyst.studio.pbix_extract",
                str(source),
                str(destination),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 180
        try:
            while process.poll() is None:
                try:
                    memory = psutil.Process(process.pid).memory_info().rss
                except psutil.NoSuchProcess:
                    break
                if memory > budget or psutil.virtual_memory().available < 256 * 1024**2:
                    raise ValueError(
                        "This model needs more memory to decode. Close unused apps and retry; "
                        "the original report is unchanged."
                    )
                if time.monotonic() > deadline:
                    raise ValueError(
                        "Model extraction reached its three-minute limit. "
                        "Open this report in Power BI Desktop."
                    )
                if shutil.disk_usage(destination).free < 512 * 1024**2:
                    raise ValueError("Not enough free disk space to extract this model")
                time.sleep(0.1)
            if process.wait() != 0:
                raise ValueError(
                    "The embedded model could not be decoded. It may use an unsupported "
                    "storage version. Open it in Power BI Desktop."
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
    model: dict[str, Any] = json.loads((destination / "model.json").read_text(encoding="utf-8"))
    return model


def import_report(
    path: str, state_root: Path, workspace_id: str, ingestion: StudioIngestionService
) -> tuple[ImportedReport, DatasetSnapshot | None]:
    source = Path(path).expanduser().resolve(strict=True)
    if (
        not source.is_file()
        or source.suffix.casefold() != ".pbix"
        or source.stat().st_size > 512 * 1024**2
    ):
        raise ValueError("Choose a PBIX file up to 512 MiB")
    if shutil.disk_usage(state_root).free < source.stat().st_size * 4 + 1024**3:
        raise ValueError("Not enough free disk space to import this report")
    report_id = f"report-{uuid.uuid4().hex}"
    root = state_root / "reports" / report_id
    root.mkdir(parents=True)
    original = root / "original.pbix"
    source_hash = sha256_file(source)
    shutil.copyfile(source, original)
    if sha256_file(original) != source_hash:
        raise ValueError("The source report changed while it was being copied; retry the import")
    pages, format_name, has_model = read_report(original)
    warnings = []
    snapshot = None
    metadata: dict[str, Any] = {}
    if has_model:
        metadata, snapshot, decoded_warnings = _decode(
            original, root / "decoded", workspace_id, ingestion
        )
        warnings.extend(decoded_warnings)
    else:
        warnings.append(
            "This report has no embedded local model. Pages are available; it may use a "
            "remote model. Open it in Power BI Desktop to view values. No external "
            "connection was opened."
        )
    raw_relationships = metadata.get("relationships", [])
    facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else None
    model, snapshot_model = None, None
    if facts is not None:
        key = model_key(original)
        store_facts(root, key, facts)
        model = model_facts.stored(facts)
        snapshot_model = model_facts.digest(facts, key) if snapshot is not None else None
    report = ImportedReport(
        id=report_id,
        workspace_id=workspace_id,
        name=source.stem,
        source_path=str(source),
        archived_path=str(original),
        source_hash=source_hash,
        base_hash=source_hash,
        format=format_name,
        pages=pages,
        tables=metadata.get("schema", []),
        measures=metadata.get("measures", []),
        relationships=raw_relationships,
        snapshot_id=snapshot.id if snapshot else None,
        warnings=warnings,
        relationships_version=(
            1 if metadata.get("relationships_source") == "pbixray" else RELATIONSHIPS_VERSION
        ),
        pages_version=PAGES_VERSION,
        model=model,
        snapshot_model=snapshot_model,
        model_read_version=1,
    )
    return report, snapshot


def _decode(
    archive: Path, decoded: Path, workspace_id: str, ingestion: StudioIngestionService
) -> tuple[dict[str, Any], DatasetSnapshot | None, list[str]]:
    """Decode the model in ``archive`` into ``decoded`` and ingest its rows as a new snapshot:
    (the extraction's metadata, the snapshot or None without rows, warnings)."""
    decoded.mkdir()
    metadata = extract_model(archive, decoded)
    warnings: list[str] = []
    files = []
    names = {}
    snapshot = None
    for index, table in enumerate(metadata["tables"]):
        file = decoded / table["file"]
        names[str(file)] = table["name"]
        files.append(
            SourceInventoryItem(
                id=f"pbix-table-{index}",
                name=file.name,
                relative_path=file.name,
                absolute_path=str(file),
                size_bytes=file.stat().st_size,
                modified_at=datetime.now(UTC),
                detected_format="parquet",
                selectable=True,
            )
        )
    if files:
        snapshot, _ = ingestion.ingest(workspace_id, files, table_names=names)
    else:
        warnings.append("This report has model metadata but no embedded table rows.")
    raw_relationships = metadata.get("relationships", [])
    if snapshot is not None and raw_relationships:
        # A join that silently disappears makes answers across tables wrong, so say so.
        matched = len(_relationship_proposals(raw_relationships, snapshot))
        if matched != len(raw_relationships):
            warnings.append(
                f"{len(raw_relationships) - matched} of {len(raw_relationships)} relationships "
                "in the original model point to tables or columns without imported rows, so "
                "local answers cannot use them. Power BI Desktop still has the full model."
            )
    return metadata, snapshot, warnings


def reread_data(
    report: ImportedReport, state_root: Path, ingestion: StudioIngestionService
) -> tuple[ImportedReport, DatasetSnapshot | None]:
    """Decode the head's model again into a new snapshot: after a refresh in Power BI Desktop
    the rows Studio read at import are stale (``model_facts.data_status``).

    Runs the import's own extraction (a bounded child process) on the head file, after checking
    its hash, into a new ``decoded-<id>`` folder beside the history; the old snapshot is left
    for the caller to retire. Returns the report with the new snapshot id, the head's schema,
    measures, relationships and facts, and the new snapshot (None when the head has no rows).
    Nothing is stored: the caller stores both and makes the snapshot the workspace's active one.
    """
    archive, digest = report_archive(report)
    if sha256_file(archive) != digest:
        raise ValueError(
            "This version of the report changed on disk since it was saved. "
            "Restore an earlier version or reopen the report."
        )
    root = Path(report.archived_path).parent
    metadata, snapshot, _ = _decode(
        archive, root / f"decoded-{uuid.uuid4().hex[:12]}", report.workspace_id, ingestion
    )
    facts = metadata.get("facts") if isinstance(metadata.get("facts"), dict) else None
    key = model_key(archive)
    update: dict[str, Any] = {
        "tables": metadata.get("schema", []),
        "measures": metadata.get("measures", []),
        "relationships": metadata.get("relationships", []),
        "relationships_version": (
            1 if metadata.get("relationships_source") == "pbixray" else RELATIONSHIPS_VERSION
        ),
        "snapshot_id": snapshot.id if snapshot is not None else None,
        "model": model_facts.stored(facts) if facts is not None else None,
        "snapshot_model": (
            model_facts.digest(facts, key) if facts is not None and snapshot is not None else None
        ),
        "model_read_version": max(1, report.model_read_version),
    }
    if facts is not None:
        store_facts(root, key, facts)
    return report.model_copy(update=update), snapshot


# The full facts of each model a report has had, one file per model_key beside the history, so
# moving the head back to a model Studio has read (an undo of a model change) reads no model.
FACTS_DIR = "model-facts"
FACTS_KEEP = 8


def _facts_file(root: Path, key: str) -> Path:
    return root / FACTS_DIR / f"{key}.json"


def store_facts(root: Path, key: str, facts: dict[str, Any]) -> None:
    """Keep ``facts`` for the model ``key`` of the report in ``root`` (best effort: the facts
    can always be read again). The oldest files beyond ``FACTS_KEEP`` are removed."""
    if key == "none":
        return
    target = _facts_file(root, key)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f".{uuid.uuid4().hex[:8]}.tmp")
        temporary.write_text(
            json.dumps({"v": model_facts.FACTS_VERSION, "model_key": key, "facts": facts}),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        kept = sorted(
            target.parent.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True
        )
        for old in kept[FACTS_KEEP:]:
            old.unlink(missing_ok=True)
    except OSError:
        return


def cached_facts(root: Path, key: str) -> dict[str, Any] | None:
    """The facts kept for model ``key`` of the report in ``root``, None when there are none."""
    try:
        stored = json.loads(_facts_file(root, key).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(stored, dict)
        and stored.get("model_key") == key
        and stored.get("v") == model_facts.FACTS_VERSION
        and isinstance(stored.get("facts"), dict)
    ):
        facts: dict[str, Any] = stored["facts"]
        return facts
    return None


def refresh_from_revision(report: ImportedReport, path: Path, blob: str) -> ImportedReport:
    """The report as ``path`` (a whole PBIX with sha256 ``blob``) holds it, pages re-derived.

    Pure: nothing is written and ``report`` is not changed. The service calls it when the
    head moves (an edit, an undo, a Desktop save) and stores the result, so every reader
    that draws the report sees the head. The file is hashed first: a revision whose bytes
    are not what the history says must not become the report.
    """
    if sha256_file(path) != blob:
        raise ValueError(
            "This version of the report changed on disk since it was saved. "
            "Restore an earlier version or reopen the report."
        )
    pages, format_name, _ = read_report(path)
    original = blob == report.source_hash
    return report.model_copy(
        update={
            "pages": pages,
            "format": format_name,
            "base_hash": blob,
            "revision_path": None if original else str(path),
            "revision_hash": None if original else blob,
            "pages_version": PAGES_VERSION,
        }
    )


def with_model_facts(
    report: ImportedReport,
    facts: dict[str, Any] | None,
    *,
    snapshot_facts: tuple[str, dict[str, Any]] | None = None,
) -> ImportedReport:
    """The report describing the model whose facts are ``facts`` (its head's): ``tables`` and
    ``relationships`` derived from them (a section the facts lack keeps the old rows) and
    ``model`` the stored facts. ``None`` facts (the read failed) make ``model`` unknown rather
    than leave it describing another model.

    The joins are never left describing another model: without a relationships section (a
    failed read, or a read that could not give them) ``relationships_version`` drops to 0, so
    ``report_relationships`` reads the head's joins again and, if that fails too, every reader
    is told they may be incomplete (the engine refuses rather than filter with old joins).

    ``snapshot_facts`` is ``(model_key, facts)`` of the model the snapshot's rows were decoded
    from; it fills a digest the report does not have yet (a report imported before digests
    were kept). Every attempted read marks ``model_read_version`` even when facts are unknown,
    so local evaluation cannot silently fall back to a legacy model. Pure.
    """
    read_version = max(1, report.model_read_version)
    if facts is None:
        return report.model_copy(
            update={"model": None, "relationships_version": 0, "model_read_version": read_version}
        )
    update: dict[str, Any] = {
        "model": model_facts.stored(facts),
        "model_read_version": read_version,
    }
    schema = model_facts.schema_rows(facts)
    if schema is not None:
        update["tables"] = schema
    relationships = model_facts.relationship_rows(facts)
    if relationships is not None:
        update["relationships"] = relationships
        update["relationships_version"] = RELATIONSHIPS_VERSION
    else:
        update["relationships_version"] = 0
    if report.snapshot_model is None and report.snapshot_id and snapshot_facts is not None:
        key, source = snapshot_facts
        update["snapshot_model"] = model_facts.digest(source, key)
    return report.model_copy(update=update)


def report_pages(report: ImportedReport) -> list[ImportedPage]:
    """The report's pages as the current importer reads them.

    A report stored before pages carried z, hidden flags, captions and colors is read
    again from its head file, once per file, so it need not be re-imported; if that file
    cannot be read the stored pages are returned as they are.
    """
    if report.pages_version >= PAGES_VERSION:
        return report.pages
    archive, digest = report_archive(report)
    found = _archived_pages(str(archive), digest)
    return report.pages if found is None else list(found)


@lru_cache(maxsize=4)
def _archived_pages(archive: str, digest: str) -> tuple[ImportedPage, ...] | None:
    # Caller-supplied hash keys the cache: a replaced archive is a different report.
    try:
        return tuple(read_report(Path(archive))[0])
    except (OSError, ValueError, zipfile.BadZipFile):
        return None


def model_key(report: ImportedReport | Path | str) -> str:
    """Identifies the model part of the report's head file: its CRC32 and size.

    Read from the zip's central directory (no data is decompressed). Studio's own
    revisions copy the DataModel byte for byte, so every report-layer edit keeps the key
    and whatever was derived from the model (number formats, styles) stays valid; a
    Desktop save rewrites the model and changes it. ``"none"`` for a report without one.
    """
    if isinstance(report, ImportedReport):
        archive, digest = report_archive(report)
        return _cached_model_key(str(archive), digest)
    return _read_model_key(str(report))


@lru_cache(maxsize=16)
def _cached_model_key(archive: str, digest: str) -> str:
    # Keyed by the caller's hash (the head's sha256), so a replaced file is a new key.
    return _read_model_key(archive)


def _read_model_key(archive: str) -> str:
    with zipfile.ZipFile(archive) as z:
        for entry in z.infolist():
            if entry.filename == "DataModel":
                return f"{entry.CRC:08x}-{entry.file_size}"
    return "none"


def model_fingerprint(report: ImportedReport) -> str:
    """A sha256 over what the engine's answers depend on: measures (name, table, expression,
    format string), the schema, the relationships and the snapshot.

    The visuals, formatting and pages are not part of it, so a report-layer edit, an undo
    of one, or a page reorder keeps the fingerprint and reuses a model built for it. Order
    does not matter, so a re-extraction that lists measures differently keeps it too.
    """

    def canonical(items: list[dict[str, Any]]) -> list[str]:
        return sorted(json.dumps(item, sort_keys=True, default=str) for item in items)

    measures = [
        {
            "name": m.get("Name"),
            "table": m.get("TableName"),
            "expression": m.get("Expression"),
            "format": m.get("FormatString"),
        }
        for m in report.measures
    ]
    payload: dict[str, Any] = {
        "measures": canonical(measures),
        "schema": canonical(report.tables),
        "relationships": canonical(report.relationships),
        "relationships_version": report.relationships_version,
        "snapshot": report.snapshot_id,
    }
    read_version = getattr(report, "model_read_version", 0)
    if read_version:
        payload["model_read_version"] = read_version
    # With the head's facts known, the engine also builds from its calculated DAX, lineage and
    # parameters and from the snapshot's digest (a changed calculated column, same schema row,
    # must rebuild). Absent legacy facts keep the old fingerprint; a current read attempt
    # above invalidates a cached legacy model even when it did not yield any facts.
    facts = model_facts.engine_inputs(
        getattr(report, "model", None), getattr(report, "snapshot_model", None)
    )
    if facts is not None:
        payload["model"] = facts
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def imported_measures(
    report: ImportedReport, snapshot: DatasetSnapshot
) -> list[AdvancedDaxProposal]:
    """Expose original measure source to the bounded evaluator, never as approved edits."""
    by_name = {t.table.name: t.id for t in snapshot.tables}
    return [
        AdvancedDaxProposal(
            id=f"imported_measure_{i}",
            name=m["Name"],
            expression=m["Expression"],
            table_id=by_name.get(m["TableName"], snapshot.tables[0].id),
            format_string="#,#0.00",
            assumptions=[
                "Original imported measure; local evaluation supports a bounded DAX subset."
            ],
        )
        for i, m in enumerate(report.measures)
        if m.get("Expression") and len(m["Expression"]) <= 4000 and len(m["Name"]) <= 120
    ]


def imported_relationships(
    report: ImportedReport, snapshot: DatasetSnapshot
) -> list[RelationshipProposal]:
    """The author's own joins on snapshot ids. Approved: the author already built them."""
    return _relationship_proposals(_on_snapshot(report, report_relationships(report)[0]), snapshot)


def relationships_complete(report: ImportedReport, snapshot: DatasetSnapshot) -> bool:
    """False while the joins may miss some of the author's: an older import that could not
    be read again, or joins whose tables or columns have no imported rows."""
    raw, complete = report_relationships(report)
    return complete and len(_relationship_proposals(_on_snapshot(report, raw), snapshot)) == len(
        raw
    )


def _on_snapshot(report: ImportedReport, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The head's joins under the names the snapshot's rows have: a table or column renamed in
    Desktop since the rows were read keeps its old name there (lineage tags say which)."""
    # Callers may pass a report-like object from before these fields existed.
    facts = getattr(report, "model", None)
    digest = getattr(report, "snapshot_model", None)
    if not isinstance(facts, dict) or not isinstance(digest, dict):
        return rows
    tables = {
        _name_key(str(t.get("name"))): t for t in facts.get("tables", []) if isinstance(t, dict)
    }
    columns = {
        (_name_key(str(c.get("table"))), _name_key(str(c.get("name")))): c
        for c in facts.get("columns", [])
        if isinstance(c, dict)
    }
    moved = []
    for raw in rows:
        row = dict(raw)
        for side in ("From", "To"):
            table_name, column_name = raw.get(f"{side}TableName"), raw.get(f"{side}ColumnName")
            table = tables.get(_name_key(str(table_name)))
            if table is None:
                continue
            column = columns.get((_name_key(str(table_name)), _name_key(str(column_name))))
            before = model_facts.snapshot_table(digest, table)
            if before is not None:
                row[f"{side}TableName"] = before.get("name")
            if column is not None:
                old = model_facts.snapshot_column(digest, table, column)
                if old is not None:
                    row[f"{side}ColumnName"] = old.get("name")
        moved.append(row)
    return moved


def report_relationships(report: ImportedReport) -> tuple[list[dict[str, Any]], bool]:
    """(the author's relationships, whether they are all there).

    A report imported before the extractor kept calculated-table joins is read again,
    once per archive, from its preserved copy: the user need not re-import it.
    """
    if report.relationships_version >= RELATIONSHIPS_VERSION:
        return report.relationships, True
    archive, digest = report_archive(report)
    # Keyed by the model part, not the head: a formatting edit, undo or redo writes a new
    # head around a byte-identical DataModel and must not start another extraction.
    try:
        key = _cached_model_key(str(archive), digest)
    except (OSError, zipfile.BadZipFile):
        key = "none"
    found = _archived_relationships(str(archive), key)
    if found is None:
        return report.relationships, False
    return [dict(item) for item in found], True


_RELATIONSHIP_CACHE: dict[str, tuple[dict[str, Any], ...] | None] = {}
_RELATIONSHIP_CACHE_SIZE = 8


def _archived_relationships(archive: str, model: str) -> tuple[dict[str, Any], ...] | None:
    """The joins in ``archive``, extracted once per model (``model_key``): heads that
    differ only in the report layer share the answer. A report without a model part
    (``"none"``) is never cached."""
    if model != "none" and model in _RELATIONSHIP_CACHE:
        return _RELATIONSHIP_CACHE[model]
    found = _extract_relationships(archive)
    if model != "none":
        while len(_RELATIONSHIP_CACHE) >= _RELATIONSHIP_CACHE_SIZE:
            _RELATIONSHIP_CACHE.pop(next(iter(_RELATIONSHIP_CACHE)))
        _RELATIONSHIP_CACHE[model] = found
    return found


def extract_argv(archive: str | Path, flag: str) -> list[str]:
    """The command that runs ``pbix_extract <archive> <flag>`` in a child process.

    From source or the sealed runtime ``sys.executable`` is a Python and runs the script. In the
    packaged app it is the app itself, which has no script to run: the module form is
    dispatched by ``scripts/studio_sidecar_entry.py`` (an allow-list of read-only extractors)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "-m", "ai_powerbi_analyst.studio.pbix_extract", str(archive), flag]
    return [sys.executable, str(Path(__file__).with_name("pbix_extract.py")), str(archive), flag]


# How a DataModel part begins when Power BI wrote it (pbixray's own signatures): an uncompressed
# backup, or one compressed with XPress9 by one or several threads.
STREAM_STORAGE = b"\xff\xfe" + "STREAM_STORAGE_SIGNATURE_)!@#$%^&*(".encode("utf-16-le")
_XPRESS9 = (
    "This backup was created using XPress9 compression.",
    "This backup was created using multithreaded XPrs9.",
)


def readable_model(path: str | Path) -> bool:
    """Whether ``path`` holds a DataModel Power BI wrote, read from its first 102 bytes: a
    model Studio's reader cannot open is refused without starting a child process for it."""
    try:
        with zipfile.ZipFile(path) as archive, archive.open("DataModel") as member:
            head = member.read(102)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return False
    if STREAM_STORAGE in head[:72]:
        return True
    text = head.decode("utf-16-le", errors="ignore")
    return any(signature in text for signature in _XPRESS9)


def _extract_relationships(archive: str) -> tuple[dict[str, Any], ...] | None:
    try:
        if not readable_model(archive):
            return None  # no model, or not one Power BI wrote: nothing to read
        process = subprocess.run(  # noqa: S603 - fixed local script, preserved archive, argv
            extract_argv(archive, "--relationships"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        if process.returncode or len(process.stdout) > 2_000_000:
            return None
        found = json.loads(process.stdout)
    except (OSError, ValueError, zipfile.BadZipFile, subprocess.SubprocessError):
        return None
    if not isinstance(found, list):
        return None
    return tuple(item for item in found if isinstance(item, dict))


# Power BI states cardinality as From:To. The evaluator reads from_* as the many
# side, so a one-to-many join is stored with its ends swapped.
_CARDINALITY: dict[str, Literal["many_to_one", "one_to_one", "many_to_many"]] = {
    "M:1": "many_to_one",
    "1:M": "many_to_one",
    "1:1": "one_to_one",
    "M:M": "many_to_many",
}


def _relationship_proposals(
    relationships: list[dict[str, Any]], snapshot: DatasetSnapshot
) -> list[RelationshipProposal]:
    proposals = []
    for index, raw in enumerate(relationships):
        if not isinstance(raw, dict):
            continue
        shape = str(raw.get("Cardinality", "")).upper()
        ends = [
            _snapshot_end(snapshot, raw.get(f"{side}TableName"), raw.get(f"{side}ColumnName"))
            for side in ("From", "To")
        ]
        # Unmatched names are skipped, not raised; import_report warns about the gap.
        left, right = ends
        if shape not in _CARDINALITY or left is None or right is None:
            continue
        if shape == "1:M":
            left, right = right, left
        (from_table, from_column), (to_table, to_column) = left, right
        both = str(raw.get("CrossFilteringBehavior", "")).casefold() in {"both", "2"}
        proposals.append(
            RelationshipProposal(
                id=f"imported_relationship_{index}",
                from_table_id=from_table,
                from_column_id=from_column,
                to_table_id=to_table,
                to_column_id=to_column,
                cardinality=_CARDINALITY[shape],
                cross_filter_direction="both" if both else "single",
                is_active=_flag(raw.get("IsActive"), default=True),
                rely_on_referential_integrity=_flag(raw.get("RelyOnReferentialIntegrity")),
                confidence=1.0,
                evidence=["Imported from the original Power BI model"],
                review_status="approved",
            )
        )
    return proposals


def _snapshot_end(
    snapshot: DatasetSnapshot, table_name: object, column_name: object
) -> tuple[str, str] | None:
    table = _by_name(snapshot.tables, table_name, lambda item: item.table.name)
    if table is None:
        return None
    column = _by_name(table.table.columns, column_name, lambda item: item.name)
    return None if column is None else (table.id, column.id)


def _by_name(items: list[Any], name: object, key: Callable[[Any], str]) -> Any:
    # Exact spelling first; case and spacing decide only when that is unambiguous.
    if not isinstance(name, str) or not name.strip():
        return None
    exact = [item for item in items if key(item) == name]
    if len(exact) == 1:
        return exact[0]
    folded = [item for item in items if _name_key(key(item)) == _name_key(name)]
    return folded[0] if len(folded) == 1 else None


def _name_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _flag(value: object, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return bool(value)
