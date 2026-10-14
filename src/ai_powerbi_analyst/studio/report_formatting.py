"""Bounded presentation metadata for the local renderer, never executable markup.

``visual_format`` turns one visual's PBIR formatting into typed tokens (title, labels,
legend, axes, series colors, container, tile, table, textbox, shape, image) plus the
``fidelity`` of the drawing and the properties it does not draw. The payload contract the
canvas codes against is docs/imported-report-render-contract.md.
"""

from __future__ import annotations

import base64
import copy
import json
import os
import re
import subprocess
import sys
import threading
import zipfile
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from . import report_tokens as tokens
from .render_analytics import (
    Calculation,
    grid_layout,
    line_specs,
    parse_calculation,
    trend_splits,
)
from .render_rules import (
    Resolve,
    Rule,
    RuleUnsupported,
    color_rule,
    expression_of,
    icon_rule,
    is_rule,
)
from .report_import import ImportedVisual, model_key
from .report_props import (
    WILDCARD_ROW,
    alpha_of,
    color,
    flag,
    number,
    palette_of,
    properties,
    px,
    safe_color,
    text,
    theme_defaults,
    value,
)
from .report_theme import (
    DEFAULT_THEME,
    image_data_uri,
    literal_image_uri,
    merged_theme,
    read_report_config,
    text_class,
    theme_palette,
    theme_payload,
)
from .report_tokens import (
    RENDERED,
    FormatContext,
    fidelity_of,
    property_is_rendered,
    unrendered_properties,
)

__all__ = [
    "RENDERED",
    "FormatContext",
    "color",
    "data_formatting",
    "data_model_key",
    "format_context",
    "formatting_helpers",
    "model_formats",
    "page_style",
    "properties",
    "property_is_rendered",
    "safe_color",
    "safe_svg",
    "theme",
    "theme_payload",
    "value",
    "visual_format",
]

Json = dict[str, Any]


class _FieldResolver(Protocol):
    def field(
        self, field: dict[str, Any], aliases: dict[str, Any] | None = None
    ) -> tuple[str, str, str, str]: ...


def data_model_key(archive: str) -> str:
    """CRC and size of the DataModel part, read from the zip directory.

    A report-layer revision rewrites the file but copies the DataModel byte for byte, so
    the key stays put across revisions and only a model change moves it.
    """
    return model_key(archive)


_FORMATS: OrderedDict[str, dict[tuple[str, str], str]] = OrderedDict()
_FORMATS_LOCK = threading.Lock()


def model_formats(archive: str, source_hash: str) -> dict[tuple[str, str], str]:
    """Number formats of the model's measures, cached by the model, not by the file.

    The caller verifies the source hash before using this. Isolated extraction bounds the
    lifetime of PBIXRay's decompression buffers for large models.
    """
    key = data_model_key(archive)
    if key == "none":
        return {}
    with _FORMATS_LOCK:
        cached = _FORMATS.get(key)
        if cached is not None:
            _FORMATS.move_to_end(key)
            return cached
    proc = subprocess.run(  # noqa: S603 - fixed local script, verified archive, argv only
        [sys.executable, str(Path(__file__).with_name("pbix_extract.py")), archive, "--formats"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=90,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode or len(proc.stdout) > 2_000_000:
        raise ValueError("Could not read the report's number formats. Retry the preview.")
    formats = {
        (m["TableName"], m["Name"]): m["FormatString"]
        for m in json.loads(proc.stdout)
        if isinstance(m.get("FormatString"), str) and len(m["FormatString"]) <= 500
    }
    with _FORMATS_LOCK:
        _FORMATS[key] = formats
        while len(_FORMATS) > 8:
            _FORMATS.popitem(last=False)
    return formats


def theme(archive: str, blob: str) -> Json:
    """The report's effective theme: base theme with the custom theme merged over it.

    Dicts merge per key (so ``textClasses`` and ``visualStyles`` keep the base theme's
    entries the custom theme does not set), lists and scalars are replaced. ``blob`` is the
    sha256 of ``archive``: a theme can change with any revision, so it keys the cache, not
    the data model.
    """
    return merged_theme(archive, blob)


def format_context(archive: str, blob: str) -> FormatContext:
    """Theme, ColorId palette and image resolver of one report file."""
    merged = theme(archive, blob)
    images: dict[tuple[str, str], str | None] = {}

    def clean(uri: str | None, mime: str | None) -> str | None:
        # SVG is markup: only the sanitizer's rebuilt copy leaves this module.
        if uri and mime == "image/svg+xml":
            return safe_svg(uri)
        return uri or None

    def image(package: Any, item: Any) -> str | None:
        key = (str(package), str(item))
        if key not in images:
            try:
                with zipfile.ZipFile(archive) as z:
                    images[key] = clean(*image_data_uri(z, read_report_config(z), package, item))
            except (OSError, zipfile.BadZipFile, ValueError):
                images[key] = None
        return images[key]

    def literal_image(uri: str) -> str | None:
        return clean(*literal_image_uri(uri))

    return FormatContext(merged, theme_palette(merged.get("dataColors", [])), image, literal_image)


def _default_context(source: Any) -> FormatContext:
    """A context for callers that hold only a palette list or a theme dict."""
    if isinstance(source, Mapping):
        return FormatContext(dict(source), palette_of(source))
    base = copy.deepcopy(DEFAULT_THEME)
    return FormatContext(base, palette_of(source))


@lru_cache(maxsize=8)
def page_style(archive: str, source_hash: str, page_id: str) -> tuple[Json, list[str]]:
    """The page canvas: background color and image, opacity, wallpaper; and the palette.

    ``source_hash`` is the sha256 of ``archive`` (the head revision's), so an edited
    background is read again. The page's background transparency (percent) becomes
    ``alpha``; a page with no background object takes the theme's (100 for the base theme,
    which shows the wallpaper).
    """
    merged = theme(archive, source_hash)
    palette = theme_palette(merged.get("dataColors", []))
    with zipfile.ZipFile(archive) as z:
        config = json.loads(z.read(f"Report/definition/pages/{page_id}/page.json"))
        report = read_report_config(z)
        objects = config.get("objects", {})
        props = properties(objects, "background")
        page_theme = theme_defaults(merged, "page", "background")
        transparency = number(props.get("transparency"))
        if transparency is None:
            transparency = number(page_theme.get("transparency"), 0.0)
        style: Json = {
            "color": color(props.get("color"), palette)
            or color(page_theme.get("color"), palette)
            or "#ffffff",
            "alpha": alpha_of(transparency),
        }
        outspace = properties(objects, "outspace")
        wall_theme = theme_defaults(merged, "page", "outspace")
        style["wallpaper"] = {
            "color": color(outspace.get("color"), palette)
            or color(wall_theme.get("color"), palette)
            or "#ffffff",
            "alpha": alpha_of(number(outspace.get("transparency"), 0.0)),
        }
        image = props.get("image", {}).get("image", {})
        resource = image.get("url", {}).get("expr", {}).get("ResourcePackageItem", {})
        uri, mime = image_data_uri(z, report, resource.get("PackageName"), resource.get("ItemName"))
        if uri and mime in {"image/png", "image/jpeg", "image/gif"}:
            style["image"] = uri
            style["scaling"] = value(image.get("scaling"), "Fit")
    return style, palette


def group_format(visual: ImportedVisual, context: FormatContext) -> Json:
    """A visual group frames nothing itself: no title, box or border of its own, only the
    background it may paint behind its members."""
    background = tokens.group_background(visual.definition, context.palette)
    return {
        "container": {
            "background": background,
            "border": {"show": False, "color": None, "width": None, "radius": 0.0},
            "shadow": False,
            "padding": None,
        },
        "titleVisible": False,
        "fidelity": "exact",
        "unrendered": [],
    }


def _order_of(selector: Any) -> float:
    found = selector.get("order") if isinstance(selector, dict) else None
    return float(found) if isinstance(found, int | float) and not isinstance(found, bool) else 0.0


def _rule_font(objects: Mapping[str, Any], query_ref: str) -> Any:
    """The measure expression of a column's rule-driven font color: a measure scoped to
    the column's value cells."""
    found = properties(objects, "values", query_ref, data=[WILDCARD_ROW]).get("fontColor")
    return tokens.walk(found, "solid", "color")


def formatting_helpers(
    visual_type: str, body: Mapping[str, Any], query_refs: list[str]
) -> list[Any]:
    """The measure expressions the formatting adds to a visual's query: the picture of a new
    card, its reference labels and their details, and rule-driven column font colors.

    ``visual_format`` appends them to the visual as hidden fields, so they change its
    columns and its query; the data key of ``live_report`` hashes them. A measure named in
    a property that is switched off adds nothing.
    """
    raw = body.get("objects")
    objects: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    first = query_refs[0] if query_refs else None
    nodes: list[Any] = []
    if visual_type == "cardVisual":
        image = properties(objects, "image", first)
        if value(image.get("show"), False):
            nodes.append(image.get("imageUrl"))
        for entry in tokens.entries_of(objects, "referenceLabel"):
            selector = entry.get("selector") or {}
            if selector.get("metadata") != first:
                continue
            nodes.append((entry.get("properties") or {}).get("value"))
            detail = properties(objects, "referenceLabelDetail", first, selector.get("id"))
            if value(detail.get("show"), True):
                nodes.append(detail.get("detailValue"))
    nodes.extend(_rule_font(objects, ref) for ref in query_refs)
    return [n for n in nodes if isinstance(tokens.walk(n, "expr", "Measure"), dict)]


# The container properties a measure can drive (Desktop's fx on a title or background). For
# a visual with a query the measure is evaluated once for the whole visual (``dynamic``).
DYNAMIC_CONTAINER: tuple[tuple[str, str, str], ...] = (
    ("title.text", "title", "text"),
    ("title.fontColor", "title", "fontColor"),
    ("title.background", "title", "background"),
    ("background.color", "background", "color"),
)
# Query aggregate functions (semanticQuery QueryAggregateFunction) the local engine computes.
_AGGREGATES = {0: "SUM", 1: "AVERAGE", 2: "DISTINCTCOUNT", 3: "MIN", 4: "MAX", 5: "COUNT"}


def _column_entries(objects: Mapping[str, Any], name: str, query_ref: str, *, cells: bool) -> Json:
    """Properties an object sets for one column: entries whose selector names the column,
    later entries winning. ``cells`` asks for the "every value cell" scope ({data:
    [dataViewWildcard], metadata}) a table's conditional formats use; otherwise the
    column scope ({metadata}) of ``columnFormatting`` and ``general``."""
    merged: Json = {}
    for entry in tokens.entries_of(objects, name):
        selector = entry.get("selector") or {}
        if selector.get("metadata") != query_ref:
            continue
        kind = tokens.selector_kind(selector)
        if (cells and kind == "data+metadata" and tokens.rule_scope(selector)) or (
            not cells and kind == "metadata"
        ):
            merged.update(entry.get("properties") or {})
    return merged


def _container_node(body: Mapping[str, Any], name: str, key: str) -> Any:
    holder = body.get("visualContainerObjects") or body.get("vcObjects")
    return properties(holder if isinstance(holder, dict) else {}, name).get(key)


def data_formatting(visual_type: str, body: Mapping[str, Any], query_refs: list[str]) -> list[Any]:
    """Everything in a visual's formatting that changes its rows, as the data key hashes it.

    The helper measures of ``formatting_helpers``, every conditional format (a rule's colors
    are computed into the rows), the measures behind a dynamic title or background, the
    visual-level number formats (they format ``formatted_rows``), the analytics lines, and
    a table's totals switch. A title, a plain color or a position is not in it.
    """
    raw = body.get("objects")
    objects: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
    first = query_refs[0] if query_refs else None
    found: list[Any] = list(formatting_helpers(visual_type, body, query_refs))
    family = tokens.family_of(visual_type)
    for ref in query_refs:
        cell = _column_entries(objects, "values", ref, cells=True)
        column = _column_entries(objects, "columnFormatting", ref, cells=False)
        general = _column_entries(objects, "general", ref, cells=False)
        found.append(
            [
                ref,
                {k: cell[k] for k in ("fontColor", "backColor") if is_rule(cell.get(k))},
                cell.get("icon"),
                {k: column[k] for k in ("fontColor", "backColor") if is_rule(column.get(k))},
                {k: column[k] for k in ("labelDisplayUnits", "labelPrecision") if k in column},
                general.get("formatString"),
            ]
        )
    if family == "chart":
        found.append(
            [
                [e.get("selector"), (e.get("properties") or {}).get("fill")]
                for e in tokens.entries_of(objects, "dataPoint")
                if is_rule((e.get("properties") or {}).get("fill"))
            ]
        )
        found.append({name: objects.get(name) for name in tokens.LINE_OBJECT_NAMES})
    elif family == "cardVisual":
        value_props = properties(objects, "value", first)
        found.append(
            [
                node
                for node in (
                    value_props.get("fontColor"),
                    properties(objects, "label", first).get("fontColor"),
                    properties(objects, "fillCustom", first).get("fillColor"),
                )
                if is_rule(node)
            ]
        )
        found.append(
            {
                k: value_props.get(k)
                for k in ("labelDisplayUnits", "labelPrecision", "customFormatString")
            }
        )
    elif family == "card":
        labels = properties(objects, "labels")
        found.append(
            [
                labels.get("color") if is_rule(labels.get("color")) else None,
                labels.get("labelDisplayUnits"),
                labels.get("labelPrecision"),
            ]
        )
    elif family == "table":
        found.append(properties(objects, "total").get("totals"))
    dynamic: list[Any] = []
    for _, name, key in DYNAMIC_CONTAINER:
        node = _container_node(body, name, key)
        if "Measure" in (expression_of(node) or {}):
            dynamic.append(node)
    found.append(dynamic)
    return found


def visual_format(
    compiler: _FieldResolver,
    visual: ImportedVisual,
    fields: list[dict[str, Any]],
    formats: Mapping[tuple[str, str], str],
    theme_source: Any = None,
    *,
    context: FormatContext | None = None,
) -> Json:
    """Typed presentation settings, validated auxiliary measure fields and the fidelity.

    ``theme_source`` is the effective theme dict or, for older callers, a ColorId palette;
    ``context`` (see ``format_context``) adds report images. Unsupported visuals get the
    box, title and container tokens only. The returned dict also carries ``fidelity`` and
    ``unrendered``; ``live_report`` lifts them to the visual.
    """
    ctx = context or _default_context(theme_source)
    if visual.container_kind == "group":
        return group_format(visual, ctx)
    raw_body = visual.definition.get("visual", {})
    body: Json = raw_body if isinstance(raw_body, dict) else {}
    reader = tokens.Reader(visual.visual_type, body, ctx)
    objects = reader.objects
    family = tokens.family_of(visual.visual_type)
    first = fields[0].get("query_ref") if fields else None
    header = "slicer" if visual.visual_type in {"slicer", "advancedSlicerVisual"} else None
    title = tokens.title_token(reader, family, fields)
    # The card value's size in px: its own setting, else the theme's callout class.
    own_size = reader.number("value", "fontSize", None, metadata=first)
    callout = text_class(ctx.theme, "callout")
    fmt: Json = {
        "label": reader.text("label", "text", visual.title, metadata=first),
        "showLabel": reader.flag("label", "show", True, metadata=first),
        "showValue": reader.flag("value", "show", True, metadata=first),
        "titleVisible": title["show"],
        "fontSize": px(max(10.0, min(72.0, own_size if own_size else float(callout["fontSize"])))),
        "references": [],
        "categoryColors": [],
        "categoryAxisTitle": reader.flag("categoryAxis", "showAxisTitle", True),
        "valueAxisTitle": reader.flag("valueAxis", "showAxisTitle", True),
        "grid": reader.flag("valueAxis", "gridlineShow", True),
        "tableGrid": reader.flag("grid", "gridHorizontal", True),
        "title": title,
        "container": tokens.container_token(reader),
        "automaticTitle": title["automaticText"],
    }
    if header and fields:
        fmt["label"] = reader.text("header", "text", None) or title["text"] or fields[0]["label"]
        fmt["slicerStyle"] = "tiles" if visual.visual_type == "advancedSlicerVisual" else "dropdown"
    background = reader.own("background", container=True)
    if flag(background.get("show")) is False:
        fmt["background"] = "transparent"
    elif bg := reader.color("background", "color", None, container=True):
        fmt["background"] = bg
    if family == "chart":
        fmt.update(tokens.chart_tokens(reader, visual.visual_type))
        fmt["categoryColors"] = list(fmt["series"]["categoryColors"])
    elif family == "cardVisual":
        card = tokens.card_visual_tokens(reader, first, visual.title)
        fmt.update({"cardValue": card["value"], "cardLabel": card["label"], "tile": card["tile"]})
    elif family == "card":
        card = tokens.legacy_card_tokens(reader)
        fmt.update({"cardValue": card["value"], "cardLabel": card["label"], "tile": None})
    elif family == "table":
        fmt.update(tokens.table_tokens(reader))
    elif family in {"slicer", "advancedSlicer"}:
        slicer = tokens.slicer_tokens(reader, visual.visual_type, fmt["label"])
        fmt["tile"] = slicer["slicer"].pop("tile", None)
        fmt.update(slicer)
    elif family == "textbox":
        fmt["content"] = tokens.textbox_content(reader)
    elif family == "shape":
        fmt["shape"] = tokens.shape_token(reader)
    elif family == "image":
        # Not "image": on a new card that key already holds the measure-driven graphic.
        fmt["picture"] = tokens.image_token(reader)
    elif family in {"button", "navigator"}:
        fmt["button"] = tokens.button_token(reader, navigator=family == "navigator")

    def queried() -> int:
        return sum(1 for f in fields if f.get("kind") != "computed")

    def add_measure(expression: Json, *, soft: bool) -> int | None:
        ref, kind, table, name = compiler.field(expression)
        existing = next((i for i, f in enumerate(fields) if f.get("ref") == ref), None)
        if existing is not None:
            return existing
        if queried() >= 12:
            if soft:
                return None
            raise ValueError("This visual exceeds the 12-field formatting preview limit")
        fields.append(
            {
                "ref": ref,
                "kind": kind,
                "table": table,
                "name": name,
                "label": name,
                "role": "Formatting",
                "hidden": True,
            }
        )
        return len(fields) - 1

    def auxiliary(node: Any, *, soft: bool = False) -> int | None:
        expression = node.get("expr", {}) if isinstance(node, dict) else {}
        if "Measure" not in expression:
            return None
        return add_measure(expression, soft=soft)

    def resolve(node: Json) -> int | None:
        """The field that holds a conditional format's input in every row: a field of the
        visual, or a measure or aggregated column added as a hidden field. None when it
        cannot be added (a missing field, a function the engine lacks, the 12-field limit),
        which leaves the visual approximate rather than failing it."""
        try:
            if "Measure" in node:
                return add_measure(node, soft=True)
            if "Column" in node:
                ref = compiler.field(node)[0]
                return next((i for i, f in enumerate(fields) if f.get("ref") == ref), None)
            aggregation = node.get("Aggregation")
            if isinstance(aggregation, dict):
                return add_aggregate(aggregation)
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        return None

    def add_aggregate(aggregation: Json) -> int | None:
        raw = aggregation.get("Function")
        function = (
            _AGGREGATES.get(raw) if isinstance(raw, int) and not isinstance(raw, bool) else None
        )
        inner = aggregation.get("Expression")
        if function is None or not isinstance(inner, dict):
            return None
        ref, kind, table, name = compiler.field(inner)
        if kind != "column":
            return None
        dax = f"{function}({ref})"
        existing = next((i for i, f in enumerate(fields) if f.get("ref") == dax), None)
        if existing is not None:
            return existing
        if queried() >= 12:
            return None
        label = f"{function.title()} of {name}"
        fields.append(
            {
                "ref": dax,
                "kind": "measure",
                "table": table,
                "name": label,
                "label": label,
                "role": "Formatting",
                "hidden": True,
            }
        )
        return len(fields) - 1

    if visual.visual_type == "cardVisual":
        image = properties(objects, "image", first)
        if value(image.get("show"), False):
            fmt["imageIndex"] = auxiliary(image.get("imageUrl"))
        for entry in sorted(
            tokens.entries_of(objects, "referenceLabel"),
            key=lambda e: _order_of(e.get("selector")),
        ):
            selector = entry.get("selector") or {}
            if selector.get("metadata") != first:
                continue
            index = auxiliary((entry.get("properties") or {}).get("value"))
            if index is None:
                continue
            identity = selector.get("id")
            title_props = properties(objects, "referenceLabelTitle", first, identity)
            detail = properties(objects, "referenceLabelDetail", first, identity)
            fmt["references"].append(
                {
                    "index": index,
                    "label": value(title_props.get("titleText"), fields[index]["label"]),
                    "detailIndex": auxiliary(detail.get("detailValue"))
                    if value(detail.get("show"), True)
                    else None,
                }
            )
    shown = list(fields)
    for field in shown:
        query_ref = field.get("query_ref")
        field["format"] = formats.get((field["table"], field["name"]), "")
        if not query_ref:
            continue
        column = properties(objects, "columnFormatting", query_ref)
        bars = column.get("dataBars")
        if bars and tokens.totals_bars(bars) != 2:  # bars on the totals only: none on cells
            field["dataBars"] = {
                "positive": color(bars.get("positiveColor"), ctx.palette) or "#118dff",
                "negative": color(bars.get("negativeColor"), ctx.palette) or "#d64550",
                "hideText": value(bars.get("hideText"), False),
                "axis": color(bars.get("axisColor"), ctx.palette),
                "reverse": value(bars.get("reverseDirection"), False) is True,
                "min": number(bars.get("minValue")),
                "max": number(bars.get("maxValue")),
            }
        field["alignment"] = str(value(column.get("alignment"), "")).lower()
        field["colorIndex"] = None
    extra_unrendered: list[str] = []
    computed = _conditional_formats(reader, family, fmt, fields, shown, first, resolve)
    _number_formats(reader, family, fields, shown, first)
    _visual_calculations(reader, fields, shown)
    query = body.get("query")
    state = query.get("queryState") if isinstance(query, dict) else None
    if isinstance(state, dict) and any(
        isinstance(role, dict) and role.get("fieldParameters") for role in state.values()
    ):
        # The fields a field parameter chose when the file was saved are drawn; choosing
        # others happens in Power BI.
        reader.note("query.fieldParameters")
    if fields and family not in {"textbox", "shape", "image", "group", "unsupported"}:
        _dynamic_container(reader, fmt, fields, auxiliary)
    if family in {"chart", "cardVisual"}:
        index = next(
            (
                i
                for i, f in enumerate(shown)
                if f.get("role") == "Rows" and f.get("kind") == "column"
            ),
            None,
        )
        if index is not None:
            fmt["smallMultiples"] = {
                "index": index,
                "field": shown[index].get("label"),
                "query_ref": shown[index].get("query_ref"),
                "layout": grid_layout(properties(objects, "smallMultiplesLayout")),
            }
            fmt["_smallMultiples"] = index
    if family == "chart":
        lines, not_drawn = line_specs(objects, ctx.palette)
        for path in not_drawn:
            reader.note(path)
        drawn = []
        for spec in lines:
            if spec.measure is not None:
                # A constant line a measure gives: evaluated once for the whole visual.
                before = len(fields)
                try:
                    index = add_measure(spec.measure, soft=True)
                except (ValueError, KeyError, TypeError, AttributeError):
                    index = None
                if index is None:
                    reader.note(f"{spec.object}.value")
                    continue
                if index >= before:
                    fields[index]["_whole"] = True
                spec = replace(spec, value_index=index)
            drawn.append(spec)
        if drawn:
            fmt["_lines"] = drawn
        split_off = trend_splits(drawn, fields)
        extra_unrendered.extend(split_off)
    # Computed columns come last: they are not part of the query and nothing indexes past
    # them.
    for field, point in computed:
        fields.append(field)
        point(len(fields) - 1)
    series = fmt.get("series")
    if isinstance(series, dict) and isinstance(series.get("ruleColors"), list):
        series["ruleColors"] = [r for r in series["ruleColors"] if r.get("index") is not None]
    for field in fields:
        field.setdefault("format", formats.get((field["table"], field["name"]), ""))
    if family in {"chart", "card", "cardVisual", "table", "slicer", "advancedSlicer"}:
        fmt["rangeDependent"] = _range_dependent(fmt, fields)
    unrendered = sorted(
        set(unrendered_properties(visual.visual_type, body)) | set(extra_unrendered)
    )
    fmt["unrendered"] = unrendered
    fmt["fidelity"] = fidelity_of(family, unrendered, reader.approximate)
    return fmt


def _range_dependent(fmt: Json, fields: list[Json]) -> bool:
    """Whether something computed after the query depends on all of the visual's rows (a
    scale's lowest and highest value, a percent bound, an analytics line other than a
    constant, a running sum, the panels of small multiples): with more rows than Studio
    reads (``truncated``) such a value is approximate."""
    rules = [f["_rule"] for f in fields if isinstance(f.get("_rule"), Rule)]
    calcs = [f["_calc"] for f in fields if isinstance(f.get("_calc"), Calculation)]
    lines = fmt.get("_lines") or []
    return (
        any(rule.range_dependent for rule in rules)
        or any(calc.range_dependent for calc in calcs)
        or any(spec.kind not in {"constant", "measure"} for spec in lines)
        or "_smallMultiples" in fmt
    )


# Where a computed column's index goes once it is appended to the fields.
Pointer = Callable[[int], None]


def _set(target: Json, key: str) -> Pointer:
    def point(index: int) -> None:
        target[key] = index

    return point


def _conditional_formats(
    reader: tokens.Reader,
    family: str,
    fmt: Json,
    fields: list[Json],
    shown: list[Json],
    first: str | None,
    resolve: Resolve,
) -> list[tuple[Json, Pointer]]:
    """Resolve every conditional format the canvas can draw into an index of the row.

    A field-value rule points at its hidden helper measure (whose values are sanitized
    after the query); a gradient, a set of rules or icons becomes a computed column (the
    returned list, appended by the caller) evaluated per row by ``render_rules``. A rule
    Studio cannot evaluate leaves its property in the approximate list.
    """
    palette = reader.context.palette
    computed: list[tuple[Json, Pointer]] = []

    def apply(path: str, node: Any, point: Pointer, output: str = "color") -> Any:
        try:
            if output == "icon":
                found = icon_rule(node, resolve)
                rule = found[0] if found else None
            else:
                found = None
                rule = color_rule(node, resolve, palette)
        except RuleUnsupported:
            reader.note(path)
            return None
        if rule is None:
            return None
        reader.resolved(path)
        helper = fields[rule.input]
        if rule.kind == "field" and helper.get("hidden") and helper.get("role") == "Formatting":
            helper["_sanitize"] = rule.output
            point(rule.input)
        else:
            computed.append(
                (
                    {
                        "ref": None,
                        "kind": "computed",
                        "table": "",
                        "name": path,
                        "label": path,
                        "role": "Formatting",
                        "hidden": True,
                        "computed": rule.style,
                        "_rule": rule,
                    },
                    point,
                )
            )
        return found

    objects = reader.objects
    if family == "table":
        table = fmt.get("table", {})
        for field in shown:
            query_ref = field.get("query_ref")
            if not query_ref or field.get("hidden"):
                continue
            cells = _column_entries(objects, "values", query_ref, cells=True)
            column = _column_entries(objects, "columnFormatting", query_ref, cells=False)
            for key, pointer, token in (
                ("fontColor", "colorIndex", "fontColor"),
                ("backColor", "backgroundIndex", "background"),
            ):
                field.setdefault(pointer, None)
                node = cells.get(key)
                if node is None and is_rule(column.get(key)):
                    node, path = column[key], f"columnFormatting.{key}"
                else:
                    path = f"values.{key}"
                if node is None:
                    continue
                if not is_rule(node):
                    # A plain color for every cell of the column.
                    found = color(node, palette)
                    if found:
                        entry = table.setdefault("columns", {}).setdefault(
                            query_ref, tokens.empty_column()
                        )
                        entry[token] = found
                    continue
                apply(path, node, _set(field, pointer))
            icon = cells.get("icon")
            if icon is not None:
                holder: Json = {"index": None, "layout": "before", "alignment": "top"}
                found = apply("values.icon", icon, _set(holder, "index"), "icon")
                if found:
                    holder["layout"], holder["alignment"] = found[1], found[2]
                    field["icon"] = holder
    elif family == "chart":
        rules: list[Json] = []
        for entry in tokens.entries_of(objects, "dataPoint"):
            node = (entry.get("properties") or {}).get("fill")
            selector = entry.get("selector") or {}
            if not is_rule(node) or not tokens.rule_scope(selector):
                continue
            metadata = selector.get("metadata")
            rule_color: Json = {"metadata": metadata if isinstance(metadata, str) else None}
            rules.append(rule_color)
            apply("dataPoint.fill", node, _set(rule_color, "index"))
        series = fmt.get("series")
        if isinstance(series, dict):
            # Filled in when the computed columns are appended; a rule that could not be
            # evaluated has no index and is left out.
            series["ruleColors"] = rules
    elif family == "cardVisual":
        for path, name, token in (
            ("value.fontColor", "value", "cardValue"),
            ("label.fontColor", "label", "cardLabel"),
        ):
            target = fmt.get(token)
            if isinstance(target, dict):
                target["colorIndex"] = None
                node = properties(objects, name, first).get("fontColor")
                if is_rule(node):
                    apply(path, node, _set(target, "colorIndex"))
        tile = fmt.get("tile")
        default = tile.get("default") if isinstance(tile, dict) else None
        if isinstance(default, dict):
            default["fillIndex"] = None
            node = properties(objects, "fillCustom", first).get("fillColor")
            if is_rule(node):
                apply("fillCustom.fillColor", node, _set(default, "fillIndex"))
    elif family == "card":
        target = fmt.get("cardValue")
        if isinstance(target, dict):
            target["colorIndex"] = None
            node = properties(objects, "labels").get("color")
            if is_rule(node):
                apply("labels.color", node, _set(target, "colorIndex"))
    return computed


def _number_formats(
    reader: tokens.Reader, family: str, fields: list[Json], shown: list[Json], first: str | None
) -> None:
    """Visual-level formats: a field's own format string (the projection's ``format``, or
    ``general.formatString``), and the display units and decimals of a table column or a
    card value. The query formats the values with them (``formatted_rows``). Display units
    -1 is Power BI's "Custom": the format is the object's own custom format string."""
    objects = reader.objects
    for index, field in enumerate(shown):
        query_ref = field.get("query_ref")
        if not query_ref:
            continue
        general = _column_entries(objects, "general", query_ref, cells=False)
        own = field.get("_format") or text(general.get("formatString"))
        units: float | None = None
        decimals: float | None = None
        if family == "table":
            scope = _column_entries(objects, "columnFormatting", query_ref, cells=False)
            units = number(scope.get("labelDisplayUnits"))
            decimals = number(scope.get("labelPrecision"))
        elif index == 0 and family == "cardVisual":
            scope = properties(objects, "value", first)
            units = number(scope.get("labelDisplayUnits"))
            decimals = number(scope.get("labelPrecision"))
            own = text(scope.get("customFormatString")) or own
        elif index == 0 and family == "card":
            scope = properties(objects, "labels")
            units = number(scope.get("labelDisplayUnits"))
            decimals = number(scope.get("labelPrecision"))
        if own:
            field["format"] = own[:500]
        if units == -1:
            units = None
        if units is not None:
            field["displayUnits"] = units
        if decimals is not None and decimals >= 0:
            field["decimals"] = int(decimals)


def _visual_calculations(reader: tokens.Reader, fields: list[Json], shown: list[Json]) -> None:
    """Plan the visual calculations the rows can give (running sum, moving average,
    previous, percent of parent...); any other one is drawn blank and approximate."""
    levels = sum(
        1 for f in shown if f.get("kind") == "column" and f.get("role") not in {"Tooltips"}
    )
    for index, field in enumerate(shown):
        expression = field.get("_calculation")
        if field.get("kind") != "computed" or not isinstance(expression, str):
            continue
        plan = parse_calculation(expression, shown, index)
        field["computed"] = "visualCalculation"
        if plan is None:
            reader.note("visualCalculation.expression")
            continue
        if plan.kind in {"percentOfParent", "versusParent"}:
            if levels <= 1:
                # With one level on the axis the parent is the grand total: the engine's.
                total_kind = "percentOfTotal" if plan.kind == "percentOfParent" else "versusTotal"
                plan = replace(plan, kind=total_kind)
            else:
                # A parent's value summed from the rows is right only for a measure that
                # adds up (an average does not); Power BI evaluates the measure itself.
                reader.note("visualCalculation.parent")
        field["_calc"] = plan
        source = shown[plan.source]
        percent = plan.kind in {"percentOfParent", "percentOfTotal", "percentChange"}
        field["format"] = "0.00%" if percent else source.get("format", "")


def _dynamic_container(
    reader: tokens.Reader,
    fmt: Json,
    fields: list[Json],
    auxiliary: Callable[..., int | None],
) -> None:
    """A title or background whose text or color a measure gives (Desktop's fx): the
    measure is added as a hidden field and evaluated for the whole visual (the totals
    piece, never the grouped one: ``_whole``)."""
    dynamic: dict[str, int] = {}
    for path, name, key in DYNAMIC_CONTAINER:
        node = reader.own(name, container=True).get(key)
        found = expression_of(node)
        if not found or "Measure" not in found:
            continue
        before = len(fields)
        try:
            index = auxiliary({"expr": found}, soft=True)
        except (ValueError, KeyError, TypeError, AttributeError):
            index = None
        if index is None:
            reader.note(path)
            continue
        if index >= before:
            fields[index]["_whole"] = True
        dynamic[path] = index
        reader.resolved(path)
    if dynamic:
        fmt["dynamicProperties"] = sorted(dynamic)
        fmt["_dynamic"] = dynamic


def safe_svg(data: Any) -> str | None:
    """Only local, inert SVG primitives; reject scripts, links and external assets."""
    if (
        not isinstance(data, str)
        or not data.startswith("data:image/svg+xml")
        or len(data) > 100_000
    ):
        return None
    header, _, body = data.partition(",")
    try:
        xml = (
            base64.b64decode(body, validate=True).decode() if ";base64" in header else unquote(body)
        )
        if _DOCTYPE.search(xml):
            return None
        root = ET.fromstring(xml)  # noqa: S314 - bounded input, DTD/entities rejected above
        allowed_tags = {
            "svg",
            "g",
            "defs",
            "linearGradient",
            "stop",
            "rect",
            "line",
            "circle",
            "path",
            "text",
            "tspan",
            "ellipse",
            "polyline",
            "polygon",
            "clipPath",
        }
        allowed_attrs = {
            "id",
            "xmlns",
            "width",
            "height",
            "viewBox",
            "x",
            "y",
            "x1",
            "x2",
            "y1",
            "y2",
            "rx",
            "ry",
            "cx",
            "cy",
            "r",
            "d",
            "points",
            "fill",
            "stroke",
            "stroke-width",
            "stroke-dasharray",
            "opacity",
            "fill-opacity",
            "stroke-opacity",
            "offset",
            "stop-color",
            "stop-opacity",
            "gradientUnits",
            "transform",
            "font-family",
            "font-size",
            "font-weight",
            "text-anchor",
            "dominant-baseline",
            "clip-path",
            "preserveAspectRatio",
        }
        for el in root.iter():
            if el.tag.removeprefix("{http://www.w3.org/2000/svg}") not in allowed_tags:
                return None
            # Power BI SVG measures commonly encode gradient stops as CSS.
            # Convert only these two presentation properties to SVG attributes.
            for declaration in el.attrib.pop("style", "").split(";"):
                if not declaration.strip():
                    continue
                key, sep, setting = declaration.partition(":")
                if not sep or key.strip() not in {"stop-color", "stop-opacity"}:
                    return None
                el.set(key.strip(), setting.strip())
            for name, attr in el.attrib.items():
                if (
                    name not in allowed_attrs
                    or ("url(" in attr and not _LOCAL_URL.fullmatch(attr))
                    or "://" in attr
                ):
                    return None
        return "data:image/svg+xml;base64," + base64.b64encode(ET.tostring(root)).decode()
    except (ValueError, ET.ParseError, UnicodeError):
        return None


_DOCTYPE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)", re.I)
_LOCAL_URL = re.compile(r"url\(#[\w-]+\)")
