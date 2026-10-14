"""Compile imported visual bindings into bounded native queries for Studio rendering.

No report-supplied DAX is executed here: fields resolve against imported metadata,
filters use a closed expression compiler, and unsupported semantics fail visibly.

One page is one query: a piece per data visual, plus a totals piece (a table's totals row, a
dynamic title) and a highlight piece (a selection another visual highlights by) where needed.
After the query the rows are finished in Python: conditional formats, visual calculations,
analytics lines and small-multiple panels (``render_rules``, ``render_analytics``). Bookmarks,
drill values, actions and interactions come from ``render_navigation``.

The payload this module returns is specified in docs/imported-report-render-contract.md.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, StrictBool, StrictFloat, StrictInt, StrictStr, field_validator

from ai_powerbi_analyst.contracts import ClosedModel
from ai_powerbi_analyst.integrity import sha256_file

from . import model_facts
from . import render_navigation as nav
from .native_report import NativeReportPreview
from .render_analytics import (
    GRAND_TOTAL_KINDS,
    Calculation,
    evaluate_calculation,
    line_values,
    panels,
)
from .render_rules import display_format, evaluate, is_percent, with_suffix
from .report_engine import LocalReportEngine, unavailable_reference
from .report_formatting import (
    FormatContext,
    data_formatting,
    format_context,
    model_formats,
    page_style,
    safe_color,
    safe_svg,
    theme,
    theme_payload,
    visual_format,
)
from .report_import import (
    ImportedPage,
    ImportedReport,
    ImportedVisual,
    is_effectively_hidden,
    model_key,
    read_report,
    report_archive,
)
from .report_theme import theme_palette
from .report_tokens import clean_text

Json = dict[str, Any]
Value = StrictBool | StrictInt | StrictFloat | Annotated[StrictStr, Field(max_length=500)] | None


class VisualSelection(ClosedModel):
    visual_id: str = Field(min_length=1, max_length=200)
    values: list[Value] = Field(default_factory=list, max_length=100)


class DrillFilter(ClosedModel):
    """Values of one field a drillthrough or tooltip page is shown for: ``field`` is
    ``Table.Column`` (a column's query_ref)."""

    field: str = Field(min_length=3, max_length=300)
    values: list[Value] = Field(default_factory=list, max_length=100)


class DrillSource(ClosedModel):
    """Where a drillthrough or report-page tooltip was opened from. With the target page's
    "Keep all filters" on, the source page's filters, slicers and selections come along."""

    page: str = Field(min_length=1, max_length=200)
    visual: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    selections: list[VisualSelection] = Field(default_factory=list, max_length=40)


class LiveReportRequest(ClosedModel):
    selections: list[VisualSelection] = Field(default_factory=list, max_length=40)
    searches: dict[str, Annotated[str, Field(max_length=120)]] = Field(
        default_factory=dict, max_length=40
    )
    # Draw the page as this bookmark (name or display name) shows it.
    bookmark: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    # The field values a drillthrough page is opened for, or the hovered data point a
    # tooltip page is drawn for. One object or a list.
    drill: list[DrillFilter] = Field(default_factory=list, max_length=24)
    drill_source: DrillSource | None = None
    # A client that draws cross-highlighting asks for it; otherwise a selection filters the
    # visuals Power BI would highlight (what the canvas drew before it could highlight).
    highlight: bool = False

    @field_validator("drill", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        if value is None:
            return []
        return [value] if isinstance(value, dict) else value


class Unsupported(ValueError):
    """Studio's compiler cannot reproduce this report content; Power BI Desktop can.

    ``needs_desktop`` is what a caller tests to fall back to Desktop instead of failing:
    raised for a whole page (a slicer whose saved selection cannot be compiled) it means
    "render this page in Desktop"; inside a visual it becomes that visual's ``error`` with
    ``needs_desktop: true`` and the rest of the page still renders. Still a ValueError.
    """

    needs_desktop: bool = True


def quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def literal(value: Value) -> str:
    if value is None:
        return "BLANK()"
    if isinstance(value, bool):
        return "TRUE()" if value else "FALSE()"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("Choose a finite filter value")
        return str(value)
    return quoted(value)


def saved_literal(value: str) -> Value:
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.casefold() in {"true", "false", "null"}:
        return {"true": True, "false": False, "null": None}[value.casefold()]
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?[LDd]?", value):
        return float(value.rstrip("LDd"))
    raise Unsupported("This saved filter literal needs Power BI Desktop")


Predicate = tuple[str, set[str], bool]

# Shapes the page query reads that no Desktop-authored file in hand shows, and the model
# shapes it guesses from while the model facts are unknown.
UNVERIFIED_SHAPES: dict[str, str] = {
    "projection.active": (
        "A chart's Category projections are its drill hierarchy: the levels with active true "
        "are drawn (several after 'expand to next level'), the first level when none carries "
        "the flag; a table's projections carry no drill state"
    ),
    "sortByColumn": (
        "Model facts columns[].sortByColumn names the column that orders a column (Month by "
        "MonthNumber); it is queried as a hidden SortBy field and orders the axis"
    ),
    "calculation group table shape": (
        "Without model facts, a table with an Ordinal column may be a calculation group, and "
        "one with X, X Fields and X Order columns a field parameter (Desktop's defaults)"
    ),
    "calculation group slicer": (
        "A slicer whose field is a calculation group's column, with a saved or requested "
        "selection, changes every measure the slicer reaches (visualInteractions not none)"
    ),
}


class PowerBIOnly(Unsupported):
    """A field of a table only Power BI Desktop evaluates (a calculation group applies its
    items to every measure, a field parameter switches the visual's fields)."""

    def __init__(self, message: str, table: str, kind: str) -> None:
        super().__init__(message)
        self.table, self.kind = table, kind


_POWER_BI_ONLY_REASONS = {
    "calculationGroup": "This visual uses the calculation group {}, which Power BI applies",
    "fieldParameter": "This visual uses the field parameter {}, which Power BI switches",
}
_POSSIBLY_POWER_BI_ONLY_REASONS = {
    "calculationGroup": (
        "Studio has not read this report's model, and {} may be a calculation group, which "
        "Power BI applies"
    ),
    "fieldParameter": (
        "Studio has not read this report's model, and {} may be a field parameter, which "
        "Power BI switches"
    ),
}
_NAMEOF = re.compile(r"\bNAMEOF\s*\(", re.IGNORECASE)


def power_bi_only_tables(report: ImportedReport) -> dict[str, tuple[str, str]]:
    """{table: (kind, why a visual using it is drawn by Power BI Desktop)}: the calculation
    groups and field parameters of the model facts (``model_facts.power_bi_only``). Row-level
    security is not here: Desktop's own view does not apply a role either, so the engine
    agrees.

    While the facts are unknown (being read, or unreadable) nothing rules a table out, so
    this fails closed for the tables that may be one: those the facts list anyway, those
    the model of the imported rows says are one (``snapshot_model``), and, without it, those
    shaped like Desktop's (an ``Ordinal`` column; ``X``, ``X Fields`` and ``X Order``)."""
    facts = getattr(report, "model", None)
    kinds = model_facts.power_bi_only(facts if isinstance(facts, dict) else None)
    if kinds is not None:
        return _explained(kinds, _POWER_BI_ONLY_REASONS)
    listed = _listed_power_bi_only(facts if isinstance(facts, dict) else {})
    possible = {t: k for t, k in _possibly_power_bi_only(report).items() if t not in listed}
    return {
        **_explained(listed, _POWER_BI_ONLY_REASONS),
        **_explained(possible, _POSSIBLY_POWER_BI_ONLY_REASONS),
    }


def _explained(kinds: dict[str, str], reasons: dict[str, str]) -> dict[str, tuple[str, str]]:
    return {
        table: (kind, reasons[kind].format(f"'{table[:80]}'"))
        for table, kind in kinds.items()
        if table and kind in reasons
    }


def _listed_power_bi_only(facts: Json) -> dict[str, str]:
    """What partial facts do say (a read that stopped before the columns)."""
    found: dict[str, str] = {}
    for section, key, kind in (
        ("calculationGroups", "table", "calculationGroup"),
        ("tables", "name", "calculationGroup"),
        ("fieldParameters", "table", "fieldParameter"),
    ):
        items = facts.get(section)
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or (
                section == "tables" and not item.get("calculationGroup")
            ):
                continue
            table = item.get(key)
            if isinstance(table, str) and table:
                found[table] = kind
    return found


def _possibly_power_bi_only(report: ImportedReport) -> dict[str, str]:
    digest = getattr(report, "snapshot_model", None)
    entries = digest.get("tables") if isinstance(digest, dict) else None
    found: dict[str, str] = {}
    if isinstance(entries, list):
        for entry in entries:
            name = entry.get("name") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                continue
            if entry.get("source") == "calculationGroup":
                found[name] = "calculationGroup"
            elif _NAMEOF.search(str(entry.get("expression") or "")):
                found[name] = "fieldParameter"
        return found
    columns: dict[str, set[str]] = {}
    for row in report.tables:
        columns.setdefault(str(row.get("TableName")), set()).add(str(row.get("ColumnName")))
    for table, names in columns.items():
        if "Ordinal" in names:
            found[table] = "calculationGroup"
        elif any(f"{n} Fields" in names and f"{n} Order" in names for n in names):
            found[table] = "fieldParameter"
    return found


class Compiler:
    def __init__(self, report: ImportedReport):
        self.report = report
        self.columns = {(c["TableName"], c["ColumnName"]) for c in report.tables}
        self.measures = {(m["TableName"], m["Name"]) for m in report.measures}
        self.power_bi_only = power_bi_only_tables(report)

    def field(
        self, field: dict[str, Any], aliases: dict[str, Any] | None = None
    ) -> tuple[str, str, str, str]:
        for kind, allowed in (("Column", self.columns), ("Measure", self.measures)):
            if kind not in field:
                continue
            item = field[kind]
            source = item.get("Expression", {}).get("SourceRef", {})
            table = source.get("Entity") or (aliases or {}).get(source.get("Source"))
            name = item.get("Property")
            if isinstance(table, str) and table in self.power_bi_only:
                kind_name, reason = self.power_bi_only[table]
                raise PowerBIOnly(reason, table, kind_name)
            if (
                not isinstance(table, str)
                or not isinstance(name, str)
                or (table, name) not in allowed
            ):
                raise Unsupported("A visual field is missing from the imported model")
            ref = "'" + table.replace("'", "''") + "'[" + name.replace("]", "]]") + "]"
            return ref, kind.lower(), table, name
        raise Unsupported(
            "This visual uses a hierarchy, aggregation, or field parameter not yet supported"
        )

    def condition(self, node: dict[str, Any], aliases: dict[str, Any], depth: int = 0) -> Predicate:
        if depth > 12:
            raise Unsupported("This filter is too deeply nested")
        for name, op in (("And", "&&"), ("Or", "||")):
            if name in node:
                pair = node[name]
                a, ta, ma = self.condition(pair["Left"], aliases, depth + 1)
                b, tb, mb = self.condition(pair["Right"], aliases, depth + 1)
                return f"({a}) {op} ({b})", ta | tb, ma or mb
        if "Not" in node:
            text, tables, measure = self.condition(node["Not"]["Expression"], aliases, depth + 1)
            return f"NOT({text})", tables, measure
        if "Comparison" in node:
            c = node["Comparison"]
            ref, kind, table, _ = self.field(c["Left"], aliases)
            op_text = {0: "=", 1: ">", 2: ">=", 3: "<", 4: "<="}.get(c["ComparisonKind"])
            if op_text is None or "Literal" not in c["Right"]:
                raise Unsupported("This comparison filter needs Power BI Desktop")
            value = saved_literal(c["Right"]["Literal"]["Value"])
            return f"{ref} {op_text} {literal(value)}", {table}, kind == "measure"
        if "In" in node:
            item = node["In"]
            if len(item["Expressions"]) != 1 or len(item["Values"]) > 100:
                raise Unsupported("This compound saved selection needs Power BI Desktop")
            ref, kind, table, _ = self.field(item["Expressions"][0], aliases)
            values = [
                saved_literal(v[0]["Literal"]["Value"]) for v in item["Values"] if len(v) == 1
            ]
            if len(values) != len(item["Values"]):
                raise Unsupported("This compound selection needs Power BI Desktop")
            return (
                f"{ref} IN {{{','.join(literal(v) for v in values)}}}",
                {table},
                kind == "measure",
            )
        raise Unsupported("This saved filter type is not supported by the local renderer")

    def predicates(self, filter_object: dict[str, Any]) -> list[Predicate]:
        aliases = {i["Name"]: i["Entity"] for i in filter_object.get("From", [])}
        return [self.condition(w["Condition"], aliases) for w in filter_object.get("Where", [])]

    def config(self, config: dict[str, Any]) -> list[Predicate]:
        result: list[Predicate] = []
        for item in config.get("filters", []):
            if item.get("filter"):
                result.extend(self.predicates(item["filter"]))
        return result


def saved_values(saved: dict[str, Any]) -> list[Value]:
    values: list[Value] = []
    for where in saved.get("Where", []):
        node = where["Condition"]
        if "In" in node and len(node["In"]["Expressions"]) == 1:
            values.extend(saved_literal(v[0]["Literal"]["Value"]) for v in node["In"]["Values"])
        elif "Comparison" in node and node["Comparison"]["ComparisonKind"] == 0:
            values.append(saved_literal(node["Comparison"]["Right"]["Literal"]["Value"]))
        else:
            raise Unsupported("This saved slicer selection needs Power BI Desktop")
    return values


KINDS: dict[str, str] = {
    "card": "card",
    "cardVisual": "card",
    "multiRowCard": "card",
    "columnChart": "column",
    "clusteredColumnChart": "column",
    "stackedColumnChart": "column",
    "barChart": "bar",
    "clusteredBarChart": "bar",
    "stackedBarChart": "bar",
    "lineChart": "line",
    "areaChart": "area",
    "stackedAreaChart": "area",
    "pieChart": "pie",
    "donutChart": "donut",
    "tableEx": "table",
    "pivotTable": "matrix",
    "slicer": "slicer",
    "advancedSlicerVisual": "slicer",
    "textbox": "textbox",
    "shape": "shape",
    "basicShape": "shape",
    "image": "image",
    "visualGroup": "group",
    "group": "group",
}

# Buttons and navigators Studio draws and follows in view mode. Kept apart from KINDS, which
# other modules read as "the types the canvas draws from a query or as content".
NAVIGATION_KINDS: dict[str, str] = {
    "actionButton": "button",
    "pageNavigator": "navigator",
    "bookmarkNavigator": "navigator",
}


def kind_of(visual_type: str) -> str:
    """What the canvas draws for a visual type (``unsupported`` for a frame only)."""
    return KINDS.get(visual_type) or NAVIGATION_KINDS.get(visual_type) or "unsupported"


# Kinds that are drawn from a query; the rest are frames or content that need no data.
DATA_KINDS = frozenset({"card", "column", "bar", "line", "area", "pie", "donut", "table", "slicer"})
CONTENT_KINDS = frozenset({"group", "textbox", "shape", "image", "button", "navigator"})
# Kinds whose selection filters or highlights other visuals.
SOURCE_KINDS = frozenset({"slicer", "column", "bar", "line", "area", "pie", "donut", "table"})
# What a data refresh replaces; everything else in a visual is formatting.
DATA_KEYS = frozenset(
    {
        "rows",
        "formatted_rows",
        "selected",
        "truncated",
        "sort",
        "extra",
        "totals",
        "highlights",
        "referenceLines",
        "smallMultiples",
        "dynamic",
        "dataNotes",
        "declined",
    }
)

_FRIENDLY = {
    "gauge": "Gauge",
    "kpi": "KPI",
    "treemap": "Treemap",
    "scatterChart": "Scatter chart",
    "waterfallChart": "Waterfall chart",
    "funnel": "Funnel chart",
    "map": "Map",
    "filledMap": "Map",
    "shapeMap": "Map",
    "azureMap": "Map",
    "pythonVisual": "Python visual",
    "scriptVisual": "R visual",
    "actionButton": "Button",
    "pageNavigator": "Page navigator",
    "bookmarkNavigator": "Bookmark navigator",
    "hundredPercentStackedColumnChart": "100% stacked column chart",
    "hundredPercentStackedBarChart": "100% stacked bar chart",
    "lineClusteredColumnComboChart": "Combo chart",
    "lineStackedColumnComboChart": "Combo chart",
    "ribbonChart": "Ribbon chart",
    "decompositionTreeVisual": "Decomposition tree",
    "keyDriversVisual": "Key influencers",
}
MATRIX_REASON = "Matrix subtotals and hierarchy expansion are previewed in Power BI only"
GLOBAL_FILTER_ERROR = "This page filter needs Power BI Desktop"


def unsupported_reason(visual_type: str) -> str:
    """The stable sentence for a visual Studio frames but does not draw."""
    if visual_type == "pivotTable":
        return MATRIX_REASON
    name = _FRIENDLY.get(visual_type)
    if name is None:
        words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", visual_type).strip().lower()
        name = words.capitalize() if re.fullmatch(r"[a-z ]{2,40}", words) else "Custom visual"
    return f"{name} visuals are previewed in Power BI only"


def projections(compiler: Compiler, visual: ImportedVisual) -> list[dict[str, Any]]:
    """The visual's fields in well and projection order.

    A visual calculation (``NativeVisualCalculation``) is not queried: it is a ``computed``
    field the rows give after the query (``render_analytics``). A field in the Tooltips well
    is queried but not drawn (``hidden``), as Desktop shows it only on hover. A chart's
    category well with several fields is a drill hierarchy: only the levels its projections
    mark ``active`` are shown (the first level when none is marked).
    """
    state = visual.definition.get("visual", {}).get("query", {}).get("queryState", {})
    result: list[dict[str, Any]] = []
    for role, section in state.items():
        for projection in _shown_levels(visual.visual_type, role, section.get("projections", [])):
            field = projection.get("field", {})
            calculation = field.get("NativeVisualCalculation") if isinstance(field, dict) else None
            if isinstance(calculation, dict):
                name = str(calculation.get("Name") or projection.get("queryRef") or "Calculation")
                computed: dict[str, Any] = {
                    "ref": None,
                    "kind": "computed",
                    "table": "",
                    "name": name[:200],
                    "label": str(projection.get("displayName") or name)[:200],
                    "role": role,
                    "query_ref": projection.get("queryRef", name),
                    "_calculation": str(calculation.get("Expression") or "")[:4000],
                }
                if projection.get("hidden") is True:
                    computed["hidden"] = True
                result.append(computed)
                continue
            ref, kind, table, name = compiler.field(field)
            item: dict[str, Any] = {
                "ref": ref,
                "kind": kind,
                "table": table,
                "name": name,
                "label": projection.get("displayName") or projection.get("nativeQueryRef") or name,
                "role": role,
                "query_ref": projection.get("queryRef", f"{table}.{name}"),
            }
            if role == "Tooltips" or projection.get("hidden") is True:
                # A tooltip field shows on hover only; a hidden projection feeds a visual
                # calculation and is not shown.
                item["hidden"] = True
            if isinstance(projection.get("format"), str):
                # The visual's own format string for this field (it wins over the model's).
                item["_format"] = projection["format"][:500]
            result.append(item)
    queried = [f for f in result if f["kind"] != "computed"]
    if not queried or len(queried) > 12:
        raise Unsupported("This visual needs 1–12 supported field projections")
    return result


def _shown_levels(visual_type: str, role: str, projections_: Any) -> list[Any]:
    """The projections of a well that the visual shows. A chart draws one level of a drill
    hierarchy (the fields on its axis) at a time: those whose ``active`` flag is true, or
    the first one when no field carries the flag."""
    found = list(projections_) if isinstance(projections_, list) else []
    if role != "Category" or visual_type not in _DRILL_TYPES or len(found) < 2:
        return found
    flagged = [p for p in found if isinstance(p, dict) and "active" in p]
    if not flagged:
        return found[:1]
    active = [p for p in flagged if p.get("active") is True]
    return active or found[:1]


# Visual types whose category well is a drill hierarchy (KINDS' charts).
_DRILL_TYPES = frozenset(
    t for t, k in KINDS.items() if k in {"column", "bar", "line", "area", "pie", "donut"}
)


@dataclass
class PageContext:
    """Everything one page render reads from the head file, resolved once."""

    report: ImportedReport
    archive: Path
    blob: str
    page: ImportedPage
    compiler: Compiler
    formats: dict[tuple[str, str], str]
    theme: Json
    format_context: FormatContext
    page_config: Json
    report_config: Json
    # Every page of the head (navigators, actions and tooltips name them) and the bookmarks.
    pages: list[ImportedPage] = dataclass_field(default_factory=list)
    bookmarks: list[nav.Bookmark] = dataclass_field(default_factory=list)
    # The bookmark the request draws the page as, and what of it Studio could not apply.
    bookmark: nav.Bookmark | None = None
    bookmark_notes: list[str] = dataclass_field(default_factory=list)
    # The visuals a filter state of the bookmark reaches that Studio could not apply.
    bookmark_approximate: set[str] = dataclass_field(default_factory=set)
    mobile: Json | None = None
    # page.json of every page (the drillthrough targets are read from them).
    page_configs: dict[str, Json] = dataclass_field(default_factory=dict)


def _page_context(
    report: ImportedReport, page_id: str, request: LiveReportRequest | None = None
) -> PageContext:
    archive, blob = report_archive(report)
    if sha256_file(archive) != blob:
        raise ValueError("The preserved report failed its integrity check")
    pages, format_name, _ = read_report(archive)
    page = next((p for p in pages if p.id == page_id), None)
    if page is None:
        raise ValueError("This page does not belong to the imported report")
    if format_name != "PBIR":
        raise ValueError(
            "Save this report with enhanced report format in Desktop to use live Studio visuals"
        )
    if len(page.visuals) > 40:
        raise ValueError("Live pages support up to 40 visuals. Use a smaller page in Desktop.")
    formats = model_formats(str(archive), blob)
    merged = theme(str(archive), blob)
    with zipfile.ZipFile(archive) as z:
        page_config = json.loads(z.read(f"Report/definition/pages/{page.id}/page.json"))
        report_config = (
            json.loads(z.read("Report/definition/report.json"))
            if "Report/definition/report.json" in z.namelist()
            else {}
        )
        bookmarks = nav.read_bookmarks(z)
        mobile = nav.mobile_layout(z, page)
        page_configs = {
            p.id: page_config if p.id == page.id else _page_json(z, p.id) for p in pages
        }
    ctx = PageContext(
        report,
        archive,
        blob,
        page,
        Compiler(report),
        formats,
        merged,
        format_context(str(archive), blob),
        page_config,
        report_config,
        pages,
        bookmarks,
        mobile=mobile,
        page_configs=page_configs,
    )
    if request is not None and request.bookmark:
        bookmark = nav.find_bookmark(bookmarks, request.bookmark)
        applied = _apply_bookmark(page, page_config, report_config, bookmark)
        ctx.page, ctx.page_config, ctx.report_config = (
            applied.page,
            applied.page_config,
            applied.report_config,
        )
        ctx.bookmark_notes = applied.notes
        ctx.bookmark_approximate = applied.approximate
        ctx.bookmark = bookmark
    return ctx


# A visual's display modes in a bookmark other than hidden, as the note names them.
_DISPLAY_MODES = {
    "spotlight": "spotlight",
    "maximize": "focus mode",
    "maximized": "focus mode",
    "focus": "focus mode",
    "elevation": "layer order",
    "elevated": "layer order",
}


def _current_order(visual: ImportedVisual) -> list[Json] | None:
    """A visual's sort as a bookmark's exploration state writes it (None for the default)."""
    query = visual.definition.get("visual", {}).get("query", {})
    sort = query.get("sortDefinition") if isinstance(query, dict) else None
    if not isinstance(sort, dict) or sort.get("isDefaultSort") is True:
        return None
    found = [
        {
            "Direction": 1 if item.get("direction") == "Ascending" else 2,
            "Expression": item["field"],
        }
        for item in sort.get("sort") or []
        if isinstance(item, dict) and isinstance(item.get("field"), dict)
    ]
    return found or None


def _current_active(visual: ImportedVisual) -> dict[str, list[Any]]:
    """{role: fields marked active} for the wells whose projections carry the flag."""
    state = visual.definition.get("visual", {}).get("query", {}).get("queryState", {})
    found: dict[str, list[Any]] = {}
    for role, section in state.items() if isinstance(state, dict) else ():
        items = section.get("projections") if isinstance(section, dict) else None
        flagged = [p for p in items or [] if isinstance(p, dict) and "active" in p]
        if flagged:
            found[role] = [p.get("field") for p in flagged if p.get("active") is True]
    return found


def _bookmark_state_notes(bookmark: nav.Bookmark, page: ImportedPage) -> list[str]:
    """What a bookmark saved of its visuals that Studio does not draw: a spotlight, focus
    mode or layer order, and a sort, drill level or field choice that differs from what the
    visual shows now (Studio's own bookmarks save the current ones, which need no note)."""
    by_id = {v.id: v for v in page.visuals}
    modes: list[str] = []
    changed = False
    for visual_id, container in bookmark.containers(page.id).items():
        single = container.get("singleVisual")
        if not isinstance(single, dict):
            continue
        display = single.get("display")
        mode = str(display.get("mode") or "").casefold() if isinstance(display, dict) else ""
        if mode and mode != "hidden":
            what = _DISPLAY_MODES.get(mode, "display mode")
            if what not in modes:
                modes.append(what)
        visual = by_id.get(visual_id)
        if visual is None:
            continue
        # An empty saved sort is the default sort, as is a visual with no sortDefinition.
        if "orderBy" in single and (single.get("orderBy") or None) != _current_order(visual):
            changed = True
        if "activeProjections" in single and single.get("activeProjections") != _current_active(
            visual
        ):
            changed = True
        if {"expansionStates", "drillFilter"} & set(single):
            changed = True
    notes = [f"The {what} this bookmark saved is shown in Power BI only" for what in modes]
    if changed:
        notes.append(
            "The sort, drill level or field choices this bookmark saved are shown in Power BI only"
        )
    return notes


def _page_json(z: zipfile.ZipFile, page_id: str) -> Json:
    try:
        found = json.loads(z.read(f"Report/definition/pages/{page_id}/page.json"))
    except (KeyError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


BOOKMARK_FILTERS_NOTE = (
    "Some filters this bookmark saved cannot be applied in Studio, so the values can differ "
    "from Power BI's"
)


@dataclass
class AppliedBookmark:
    """A page as a bookmark shows it: its visuals, page.json and report.json, the notes on
    what Studio did not apply, and the visuals whose values are approximate because a filter
    state the bookmark saved could not be applied."""

    page: ImportedPage
    page_config: Json
    report_config: Json
    notes: list[str]
    approximate: set[str]


def _apply_bookmark(
    page: ImportedPage, page_config: Json, report_config: Json, bookmark: nav.Bookmark
) -> AppliedBookmark:
    """The page, page.json and report.json as ``bookmark`` shows them, and plain-English
    notes on what it saved that Studio does not apply.

    Applied: which visuals and groups are shown or hidden, each slicer's selection, and the
    report, page and visual filter states the bookmark saved (``byName``, ``byExpr`` and
    ``byType``). Not applied: a visual's sort, drill level, spotlight or field choices, which
    the notes name; a filter state Studio cannot apply adds a note and leaves the visuals it
    reaches approximate.
    """
    notes: list[str] = []
    display = nav.bookmark_display(bookmark, page)
    slicers = nav.bookmark_slicers(bookmark, page)
    saved = nav.bookmark_filters(bookmark, page)
    unreadable = saved["unreadable"]
    approximate: set[str] = set(unreadable["visuals"])
    visuals: list[ImportedVisual] = []
    missing: list[str] = []
    for visual in page.visuals:
        update: dict[str, Any] = {}
        if visual.id in display:
            update["hidden"] = display[visual.id]
        definition: Json | None = None
        if visual.id in slicers:
            definition = copy.deepcopy(visual.definition)
            body = definition.setdefault("visual", {})
            objects = body.setdefault("objects", {}) if isinstance(body, dict) else {}
            if isinstance(objects, dict):
                objects["general"] = copy.deepcopy(slicers[visual.id])
        if visual.id in saved["visuals"]:
            definition = definition or copy.deepcopy(visual.definition)
            definition["filterConfig"], lost, unapplied = nav.replace_filters(
                definition.get("filterConfig"), saved["visuals"][visual.id]
            )
            missing.extend(lost)
            if unapplied:
                approximate.add(visual.id)
        if definition is not None:
            update["definition"] = definition
        visuals.append(visual.model_copy(update=update) if update else visual)
    page_copy = copy.deepcopy(page_config)
    report_copy = copy.deepcopy(report_config)
    whole_page = bool(unreadable["page"] or unreadable["report"])
    if saved["page"] is not None:
        page_copy["filterConfig"], lost, unapplied = nav.replace_filters(
            page_config.get("filterConfig"), saved["page"]
        )
        missing.extend(lost)
        whole_page = whole_page or unapplied > 0
    if saved["report"] is not None:
        report_copy["filterConfig"], lost, unapplied = nav.replace_filters(
            report_config.get("filterConfig"), saved["report"]
        )
        missing.extend(lost)
        whole_page = whole_page or unapplied > 0
    if whole_page:
        approximate |= {v.id for v in page.visuals}
    if missing:
        notes.append("Some filters this bookmark saved are no longer in the report")
    if approximate:
        notes.append(BOOKMARK_FILTERS_NOTE)
    notes.extend(_bookmark_state_notes(bookmark, page))
    if bookmark.page and bookmark.page != page.id and bookmark.captures["page"]:
        notes.append("This bookmark opens another page")
    return AppliedBookmark(
        page.model_copy(update={"visuals": visuals}), page_copy, report_copy, notes, approximate
    )


def _ranks(page: ImportedPage) -> dict[str, int]:
    """Stacking rank 1..n of every visual on the page: by z, ties by tab order, then file
    order. The canvas sets zIndex to the rank, never to the raw z."""
    order = sorted(
        enumerate(page.visuals),
        key=lambda pair: (
            pair[1].z,
            pair[1].tab_order if pair[1].tab_order is not None else 0,
            pair[0],
        ),
    )
    return {visual.id: rank for rank, (_, visual) in enumerate(order, start=1)}


@dataclass
class PageScope:
    """What a page's filters and slicers do to its visuals, resolved from the request.

    Nothing here runs a query. The saved slicer selections and the user's selections become
    predicates (``contexts``), and the page and report filters are compiled once: a filter
    Studio cannot compile leaves ``global_error`` set instead of failing the page.
    """

    global_filters: list[Predicate]
    global_error: str | None
    contexts: dict[str, list[Predicate]]
    defaults: dict[str, list[Value]]
    selected: dict[str, list[Value]]
    interactions: list[Any]
    visuals: dict[str, ImportedVisual] = dataclass_field(default_factory=dict)
    # The report setting that makes cross-filtering, not highlighting, the default.
    filter_by_default: bool = False
    # Slicers on a calculation group with a selection: {slicer: why}. Every measure they
    # reach is changed by the selected calculation item, which only Power BI applies.
    calculation_groups: dict[str, str] = dataclass_field(default_factory=dict)
    # Why the page's data visuals are approximate (a filter context Studio could not bring
    # along), shown as page notes; empty when nothing is missing.
    notes: list[str] = dataclass_field(default_factory=list)


@dataclass
class QueryPlan:
    """The filters and sort one data visual's query is built from.

    ``highlights`` are the predicates of selections the visual highlights instead of being
    filtered by (a highlight piece of the query computes the highlighted values)."""

    filters: list[Predicate]
    sort: list[Json]
    highlights: list[Predicate] = dataclass_field(default_factory=list)

    @property
    def measure_filters(self) -> bool:
        """A filter on a measure: it filters the visual's rows, so a total computed with
        ROW would not be Power BI's total."""
        return any(measure for _, _, measure in self.filters)


def _filter_by_default(report_config: Json) -> bool:
    settings = report_config.get("settings")
    return isinstance(settings, dict) and settings.get("defaultFilterActionIsDataFilter") is True


KEEP_ALL_FILTERS_NOTE = (
    "The filters of the page this was opened from are not applied in Studio, so the values "
    "can differ from Power BI's"
)


def _drill_predicates(
    ctx: PageContext, request: LiveReportRequest, notes: list[str]
) -> list[Predicate]:
    """The drill values of the request as filters.

    A drillthrough page takes values for the fields it is bound to; a tooltip page, and a
    drillthrough page that keeps all filters, take any column (the hovered point, the
    selections of the page it was opened from). With "Keep all filters" on, the source
    page's filters come along from ``drill_source``; without it the page is approximate and
    ``notes`` says why.
    """
    if not request.drill:
        return []
    if ctx.page.page_type == "default":
        raise ValueError("Only drillthrough and tooltip pages take drill values")
    binding = nav.page_binding(ctx.page_config)
    bound = {f["field"] for f in binding["fields"]} if binding else set()
    keep = binding["keepAllFilters"] if binding else True
    types = {
        (str(t.get("TableName")), str(t.get("ColumnName"))): str(t.get("PandasDataType") or "")
        for t in ctx.report.tables
    }
    predicates: list[Predicate] = []
    for drill in request.drill:
        matches = [
            (table, column)
            for table, column in ctx.compiler.columns
            if f"{table}.{column}" == drill.field
        ]
        if len(matches) != 1:
            raise ValueError(f"{drill.field[:120]} is not a column of this report")
        if ctx.page.page_type == "drillthrough" and not keep and drill.field not in bound:
            raise ValueError(f"{drill.field[:120]} is not a drillthrough field of this page")
        if not drill.values:
            continue
        table, column = matches[0]
        ref = "'" + table.replace("'", "''") + "'[" + column.replace("]", "]]") + "]"
        dates = "datetime" in types.get((table, column), "")
        values = ",".join(_date_literal(v) if dates else literal(v) for v in drill.values)
        predicates.append((f"{ref} IN {{{values}}}", {table}, False))
    if keep:
        if request.drill_source is None:
            notes.append(KEEP_ALL_FILTERS_NOTE)
        else:
            predicates += _source_predicates(ctx.report, request.drill_source, notes)
    return predicates


def _source_predicates(
    report: ImportedReport, source: DrillSource, notes: list[str]
) -> list[Predicate]:
    """The filter context of the page a drillthrough or tooltip was opened from: its page
    and report filters, its slicers and the selections sent with it (those that reach the
    source visual, when named). A filter on a measure only filters the source visual's own
    rows and cannot move to another page: it is left out and noted."""
    request = LiveReportRequest(selections=source.selections)
    try:
        ctx = _page_context(report, source.page, request)
        scope = _scope(ctx, request)
    except (Unsupported, KeyError, TypeError, AttributeError):
        notes.append(KEEP_ALL_FILTERS_NOTE)
        return []
    if scope.global_error is not None:
        notes.append(KEEP_ALL_FILTERS_NOTE)
        return []
    found = list(scope.global_filters)
    target = scope.visuals.get(source.visual) if source.visual else None
    if source.visual and target is None:
        raise ValueError("The visual the drill came from is not on its page")
    if target is not None:
        try:
            found += ctx.compiler.config(target.definition.get("filterConfig", {}))
        except (Unsupported, KeyError, TypeError, AttributeError):
            notes.append(KEEP_ALL_FILTERS_NOTE)
    for origin, predicates in scope.contexts.items():
        if target is not None and origin == target.id:
            continue  # the drilled point itself is in the drill values
        if target is not None:
            how = nav.interaction(
                scope.visuals[origin],
                kind_of(scope.visuals[origin].visual_type),
                target,
                scope.interactions,
                scope.filter_by_default,
            )
            if how == "none":
                continue
        found += predicates
    kept = [p for p in found if not p[2]]
    if len(kept) != len(found):
        notes.append(KEEP_ALL_FILTERS_NOTE)
    return kept


def _unbound(page_config: Json, drilled: set[str]) -> Json:
    """page.json without the filters a drillthrough or tooltip page binds to the drilled
    fields: the drill values replace what the page saved for them."""
    binding = page_config.get("pageBinding")
    parameters = binding.get("parameters") if isinstance(binding, dict) else None
    names = {
        p.get("boundFilter")
        for p in (parameters if isinstance(parameters, list) else [])
        if isinstance(p, dict)
        and (field := nav.field_name(p.get("fieldExpr"))) is not None
        and field["field"] in drilled
    }
    config = page_config.get("filterConfig")
    if not names or not isinstance(config, dict) or not isinstance(config.get("filters"), list):
        return page_config
    filters = config["filters"]
    return {
        **page_config,
        "filterConfig": {
            **config,
            "filters": [f for f in filters if not (isinstance(f, dict) and f.get("name") in names)],
        },
    }


_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T00:00:00(?:\.0+)?)?")


def _date_literal(value: Value) -> str:
    """A drill value of a date column (the ISO text a row carries) as a DAX date."""
    if value is None:
        return "BLANK()"
    match = _ISO_DATE.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise Unsupported("Drilling on a date with a time of day needs Power BI Desktop")
    year, month, day = (int(part) for part in match.groups())
    return f"DATE({year}, {month}, {day})"


def _scope(ctx: PageContext, request: LiveReportRequest) -> PageScope:
    page, compiler = ctx.page, ctx.compiler
    # Page and report filters are compiled here but only raised inside each visual's own
    # plan: one filter Studio cannot compile (a TopN, a relative date) then marks the
    # visuals it would have filtered as "needs Power BI Desktop" instead of failing the page.
    global_filters: list[Predicate] = []
    global_error: str | None = None
    notes: list[str] = []
    drilled = {d.field for d in request.drill if d.values}
    page_config = _unbound(ctx.page_config, drilled) if drilled else ctx.page_config
    try:
        global_filters = compiler.config(page_config.get("filterConfig", {})) + compiler.config(
            ctx.report_config.get("filterConfig", {})
        )
    except (Unsupported, KeyError, TypeError, AttributeError):
        global_error = GLOBAL_FILTER_ERROR
    global_filters += _drill_predicates(ctx, request, notes)
    visual_by_id = {v.id: v for v in page.visuals}
    calculation_groups: dict[str, str] = {}
    selected = {s.visual_id: s.values for s in request.selections}
    if len(selected) != len(request.selections) or any(
        v not in visual_by_id for v in selected | request.searches
    ):
        raise ValueError("Selections must reference distinct visuals on this page")
    contexts: dict[str, list[Predicate]] = {}
    defaults_by_id: dict[str, list[Value]] = {}
    for visual in page.visuals:
        if kind_of(visual.visual_type) in CONTENT_KINDS:
            continue
        try:
            fields = projections(compiler, visual)
        except PowerBIOnly as error:
            # A field parameter's slicer only switches the fields of the visuals that use the
            # parameter, and those are drawn by Power BI Desktop anyway. A calculation group's
            # selected item changes every measure it reaches.
            chosen = selected.get(visual.id) if visual.id in selected else None
            picked = bool(chosen) if chosen is not None else _has_saved_selection(visual)
            if error.kind == "calculationGroup" and picked:
                calculation_groups[visual.id] = (
                    f"The slicer on the calculation group '{error.table[:80]}' changes the "
                    "measures of this page, which Power BI applies"
                )
            continue
        except (Unsupported, KeyError, TypeError, AttributeError) as error:
            if visual.id in selected or (
                KINDS.get(visual.visual_type) == "slicer" and _has_saved_selection(visual)
            ):
                raise Unsupported(
                    "A slicer uses an unsupported field; open this page in Power BI"
                ) from error
            continue
        groups = [f for f in fields if f["kind"] == "column"]
        if visual.id in selected:
            if not groups:
                raise ValueError("Only category fields can filter a report")
            f = groups[0]
            contexts[visual.id] = (
                [
                    (
                        f"{f['ref']} IN {{{','.join(literal(v) for v in selected[visual.id])}}}",
                        {f["table"]},
                        False,
                    )
                ]
                if selected[visual.id]
                else []
            )
        elif KINDS.get(visual.visual_type) == "slicer":
            predicates: list[Predicate] = []
            try:
                for entry in _general_entries(visual):
                    saved = entry.get("properties", {}).get("filter", {}).get("filter")
                    if saved:
                        predicates.extend(compiler.predicates(saved))
                        defaults_by_id[visual.id] = saved_values(saved)
            except (KeyError, TypeError, AttributeError, IndexError) as error:
                raise Unsupported("This slicer's saved selection needs Power BI Desktop") from error
            contexts[visual.id] = predicates
    interactions = ctx.page_config.get("visualInteractions", [])
    return PageScope(
        global_filters,
        global_error,
        contexts,
        defaults_by_id,
        {k: list(v) for k, v in selected.items()},
        interactions if isinstance(interactions, list) else [],
        visual_by_id,
        _filter_by_default(ctx.report_config),
        calculation_groups,
        list(dict.fromkeys(notes)),
    )


def _general_entries(visual: ImportedVisual) -> list[Any]:
    found = visual.definition.get("visual", {}).get("objects", {}).get("general", [])
    return found if isinstance(found, list) else []


def _has_saved_selection(visual: ImportedVisual) -> bool:
    return any(
        isinstance(entry, dict)
        and isinstance(entry.get("properties"), dict)
        and entry["properties"].get("filter")
        for entry in _general_entries(visual)
    )


def _plan(
    ctx: PageContext,
    scope: PageScope,
    visual: ImportedVisual,
    kind: str,
    fields: list[dict[str, Any]],
    request: LiveReportRequest,
) -> QueryPlan:
    """The filters and sort of one data visual, or ``Unsupported`` when Studio cannot
    reproduce them. Shared by the data render and the formatting-only render so both give
    the same error, ``needs_desktop`` and fidelity for a visual."""
    compiler = ctx.compiler
    if scope.global_error is not None:
        raise Unsupported(scope.global_error)
    measured = any(f["kind"] == "measure" for f in fields)
    for slicer, why in scope.calculation_groups.items():
        slicer_visual = scope.visuals.get(slicer)
        if slicer == visual.id or slicer_visual is None or not measured:
            continue
        reach = nav.interaction(
            slicer_visual, "slicer", visual, scope.interactions, scope.filter_by_default
        )
        if reach != "none":
            raise Unsupported(why)
    filters: list[Predicate] = list(scope.global_filters) + compiler.config(
        visual.definition.get("filterConfig", {})
    )
    highlights: list[Predicate] = []
    for source, source_predicates in scope.contexts.items():
        source_visual = scope.visuals.get(source)
        if source_visual is None or not source_predicates:
            continue
        if source == visual.id:
            if kind == "slicer":
                continue  # a slicer lists every value whatever it selects
            if request.highlight and visual.visual_type in nav.HIGHLIGHT_TYPES:
                # The chart keeps all its points and highlights the ones selected in it.
                highlights.extend(source_predicates)
            else:
                # Without highlighting, the chart shows only what is selected in it.
                filters.extend(source_predicates)
            continue
        how = nav.interaction(
            source_visual,
            kind_of(source_visual.visual_type),
            visual,
            scope.interactions,
            scope.filter_by_default,
        )
        if how == "none":
            continue
        if how == "unsupported":
            raise Unsupported("This page uses a custom visual interaction not yet supported")
        # A client that cannot draw highlights gets the filtered rows instead.
        highlighted = how == "highlight" and request.highlight
        (highlights if highlighted else filters).extend(source_predicates)
    if kind == "slicer" and request.searches.get(visual.id):
        f = fields[0]
        filters.append(
            (
                f"CONTAINSSTRING({f['ref']}, {quoted(request.searches[visual.id])})",
                {f["table"]},
                False,
            )
        )
    if any(not measure and len(tables) != 1 for _, tables, measure in filters + highlights):
        raise Unsupported("A cross-table filter needs Power BI Desktop")
    sort: list[Json] = []
    for order in (
        visual.definition["visual"].get("query", {}).get("sortDefinition", {}).get("sort", [])
    ):
        ref = compiler.field(order["field"])[0]
        index = next((i for i, f in enumerate(fields) if f["ref"] == ref), None)
        if index is None:
            raise Unsupported("The visual sorts by a hidden field not yet supported")
        sort.append({"index": index, "descending": order.get("direction") == "Descending"})
    first = next(i for i, f in enumerate(fields) if f["kind"] != "computed")
    sort = sort or [{"index": first, "descending": False}]
    # A column the model sorts by another is ordered by that one (the hidden helper).
    for order in sort:
        helper = fields[order["index"]].get("_sortBy")
        if isinstance(helper, int):
            order["index"] = helper
    return QueryPlan(filters, sort, highlights)


@dataclass
class Framed:
    """One visual as a render sees it before any query runs.

    ``item`` is the payload row; ``fields`` the fields its query computes (formatting can
    append hidden helpers and computed columns; the private ``_`` keys say how the rows are
    finished); ``plan`` its filters and sort; ``internals`` what the formatting left for
    the data render (``_lines``, ``_dynamic``, ``_smallMultiples``).
    """

    item: Json
    fields: list[dict[str, Any]] | None
    plan: QueryPlan | None
    internals: Json


def _frame(
    ctx: PageContext,
    scope: PageScope,
    visual: ImportedVisual,
    rank: int,
    request: LiveReportRequest,
) -> Framed:
    """A visual's box, caption, position and typed formatting, with no data.

    The row never raises for unsupported content: it carries ``error`` and
    ``needs_desktop`` and keeps its box.
    """
    kind = kind_of(visual.visual_type)
    page = ctx.page
    item: Json = {
        "id": visual.id,
        "title": visual.title,
        "kind": kind,
        "visual_type": visual.visual_type,
        "container_kind": visual.container_kind,
        "columns": [],
        "rows": [],
        "selected": [],
        "truncated": False,
        "position": {
            "x": visual.x,
            "y": visual.y,
            "width": visual.width,
            "height": visual.height,
        },
        "z": visual.z,
        "rank": rank,
        "hidden": is_effectively_hidden(page, visual.id),
        "hidden_own": visual.hidden,
        "parent_group": visual.parent_group,
        "group_children": list(visual.group_children),
        "fidelity": "exact",
        "unrendered": [],
        "data_key": _key_of(ctx.report, page, visual, request, ctx.page_config, ctx.report_config),
    }
    fields: list[dict[str, Any]] | None = None
    plan: QueryPlan | None = None
    try:
        if kind in CONTENT_KINDS:
            item["formatting"] = _format(ctx, visual, [])
        elif kind in {"unsupported", "matrix"}:
            try:
                fields = projections(ctx.compiler, visual)
            except (Unsupported, KeyError, TypeError, AttributeError):
                fields = []
            item["formatting"] = _format(ctx, visual, fields)
            reason = unsupported_reason(visual.visual_type)
            item["unsupported_reason"] = reason
            # Older clients only know "error"; a client that reads unsupported_reason
            # ignores it.
            item["error"] = reason
            item["fidelity"] = "none"
            fields = None
        else:
            fields = projections(ctx.compiler, visual)
            item["formatting"] = _format(ctx, visual, fields)
            if kind == "slicer" and (len(fields) != 1 or fields[0]["kind"] != "column"):
                raise Unsupported("Only a single-column slicer is supported here")
            _sort_helpers(ctx, fields)
            item["columns"] = _columns(fields)
    except _SOFT_ERRORS as error:
        _needs_desktop(item, error)
        item["columns"] = []
        fields = None
        if "formatting" not in item:
            try:
                item["formatting"] = _format(ctx, visual, [])
            except _SOFT_ERRORS:
                item["formatting"] = {}
    if fields is not None and "error" not in item and kind in DATA_KINDS:
        # The filters and sort checked here and not in the query loop, so a formatting-only
        # render reports the same error and fidelity for a visual as a data render.
        try:
            plan = _plan(ctx, scope, visual, kind, fields, request)
        except (Unsupported, KeyError, TypeError, AttributeError) as error:
            _needs_desktop(item, error)
    _lift(item)
    internals = _internals(item)
    _navigation(ctx, item, visual, kind)
    if plan is not None and fields is not None and item["fidelity"] == "exact":
        grouped = any(f["kind"] == "column" for f in fields)
        if (
            (plan.measure_filters and grouped and _needs_total(item, fields, internals))
            or scope.notes
            or visual.id in ctx.bookmark_approximate
        ):
            # A total (a table's totals row, a dynamic title of a chart) would be computed
            # without the visual's measure filter, the page lacks the filters of the page it
            # was opened from (scope.notes), or a filter state of the applied bookmark could
            # not be applied: the visual is approximate.
            item["fidelity"] = "approximate"
    return Framed(item, fields, plan, internals)


# Keys every column carries (a helper or computed column has the defaults); the others are
# present only when they apply (docs/imported-report-render-contract.md, section 2).
_COLUMN_DEFAULTS: Json = {
    "kind": "column",
    "table": "",
    "name": "",
    "label": "",
    "role": "",
    "query_ref": None,
    "format": "",
    "alignment": "",
    "colorIndex": None,
    "hidden": False,
}


def _columns(fields: list[dict[str, Any]]) -> list[Json]:
    """The payload's columns: the fields without their DAX and the private keys, with every
    always-present key filled."""
    return [
        {
            **_COLUMN_DEFAULTS,
            **{k: v for k, v in f.items() if k != "ref" and not k.startswith("_")},
        }
        for f in fields
    ]


def _sort_helpers(ctx: PageContext, fields: list[dict[str, Any]]) -> None:
    """Give each category column the model sorts by another column (Month by MonthNumber)
    that column as a hidden helper (``role: "SortBy"``): the visual's order and a visual
    calculation's axis follow it, as in Power BI. The model facts say which column sorts
    which; without them nothing changes."""
    model = getattr(ctx.report, "model", None)
    columns = model.get("columns") if isinstance(model, dict) else None
    sort_by = {
        (c.get("table"), c.get("name")): c.get("sortByColumn")
        for c in (columns if isinstance(columns, list) else [])
        if isinstance(c, dict) and isinstance(c.get("sortByColumn"), str)
    }
    if not sort_by:
        return
    for field in list(fields):
        if field["kind"] != "column" or field.get("role") == "SortBy":
            continue
        target = sort_by.get((field["table"], field["name"]))
        if not isinstance(target, str) or (field["table"], target) not in ctx.compiler.columns:
            continue
        ref = "'" + field["table"].replace("'", "''") + "'[" + target.replace("]", "]]") + "]"
        index = next((i for i, f in enumerate(fields) if f.get("ref") == ref), None)
        if index is None:
            if sum(1 for f in fields if f["kind"] != "computed") >= 12:
                continue
            fields.append(
                {
                    "ref": ref,
                    "kind": "column",
                    "table": field["table"],
                    "name": target,
                    "label": target,
                    "role": "SortBy",
                    "hidden": True,
                }
            )
            index = len(fields) - 1
        field["_sortBy"] = index


def _internals(item: Json) -> Json:
    """Take the formatting's private keys (what only a data render uses) off the payload."""
    formatting = item.get("formatting")
    if not isinstance(formatting, dict):
        return {}
    return {k: formatting.pop(k) for k in [k for k in formatting if k.startswith("_")]}


def _navigation(ctx: PageContext, item: Json, visual: ImportedVisual, kind: str) -> None:
    """What clicking or hovering the visual does in view mode: its action (a button, or any
    visual with an action), its report-page tooltip, a slicer's sync group and a
    navigator's buttons."""
    raw = visual.definition.get("visual")
    body: Json = raw if isinstance(raw, dict) else {}
    action = nav.action_of(body, ctx.pages, [b.name for b in ctx.bookmarks])
    if action is not None:
        item["action"] = action
    tooltip = nav.tooltip_page(body, ctx.pages)
    if tooltip is not None:
        item["tooltipPage"] = tooltip
    if kind == "slicer":
        group = nav.sync_group(body)
        if group is not None:
            item["syncGroup"] = group
    formatting = item.get("formatting")
    if kind == "navigator" and isinstance(formatting, dict):
        formatting["navigator"] = {
            "kind": "pages" if visual.visual_type == "pageNavigator" else "bookmarks",
            "items": nav.navigator_items(
                visual,
                ctx.pages,
                ctx.bookmarks,
                ctx.page.id,
                ctx.bookmark.name if ctx.bookmark is not None else None,
            ),
        }


def _needs_total(item: Json, fields: Sequence[Json], internals: Json) -> bool:
    """Whether the data render needs the visual's values for the whole visual (the totals
    piece): a table's totals row, a dynamic title or background, a constant line a measure
    gives, or a visual calculation relative to the grand total."""
    formatting = item.get("formatting")
    table = formatting.get("table") if isinstance(formatting, dict) else None
    total = table.get("total") if isinstance(table, dict) else None
    shown = [f for f in fields if f["kind"] != "computed"]
    if (
        isinstance(total, dict)
        and total.get("show")
        and any(f["kind"] == "measure" and not f.get("hidden") for f in shown)
    ):
        return True
    lines = internals.get("_lines") or []
    calcs = [f["_calc"] for f in fields if isinstance(f.get("_calc"), Calculation)]
    return (
        bool(internals.get("_dynamic"))
        or any(spec.value_index is not None for spec in lines)
        or any(c.kind in GRAND_TOTAL_KINDS for c in calcs)
    )


# What a malformed or unsupported visual definition can raise while it is read. A visual that
# raises any of them is marked "needs Power BI Desktop" and the rest of the page still renders.
_SOFT_ERRORS = (Unsupported, KeyError, TypeError, AttributeError, ValueError)


def _needs_desktop(item: Json, error: Exception) -> None:
    item["error"] = (
        str(error)
        if isinstance(error, Unsupported)
        else "This visual definition needs additional local support"
    )
    item["needs_desktop"] = True
    item["fidelity"] = "none"


def _format(ctx: PageContext, visual: ImportedVisual, fields: list[dict[str, Any]]) -> Json:
    return visual_format(
        ctx.compiler, visual, fields, ctx.formats, ctx.theme, context=ctx.format_context
    )


def _lift(item: Json) -> None:
    """Move the fidelity signal out of the formatting and adopt the automatic title."""
    formatting = item.get("formatting")
    if not isinstance(formatting, dict):
        return
    unrendered = formatting.pop("unrendered", None)
    fidelity = formatting.pop("fidelity", None)
    if isinstance(unrendered, list):
        item["unrendered"] = unrendered
    # An error or a Power BI only visual is "none" whatever its formatting says.
    if isinstance(fidelity, str) and item.get("fidelity") != "none":
        item["fidelity"] = fidelity
    title = formatting.get("title")
    if isinstance(title, dict) and title.get("textSource") == "automatic" and title.get("text"):
        item["title"] = title["text"]


def compile_page(
    report: ImportedReport,
    page_id: str,
    request: LiveReportRequest,
    only: set[str] | None = None,
    extra: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """The page's visuals and one query computing them; ``only`` limits it to some visuals
    (still filtered by the page's slicers). ``extra`` adds DAX expressions to one card,
    evaluated under that card's filters, returned as its ``extra`` values."""
    compiled = _compile(report, page_id, request, only, extra)
    return compiled.visuals, compiled.query


@dataclass
class VisualQuery:
    """One data visual's part of a page render: its piece of the page query (``main``) and
    the extra pieces (a totals piece, a highlight piece) that run as a second query, so one
    that fails costs only that visual's totals or highlights."""

    main: str
    extras: list[str] = dataclass_field(default_factory=list)
    extra_rows: int = 0
    # The extra pieces read something the local model lacks: they were dropped.
    lost: bool = False


@dataclass
class Compiled:
    """A page compiled for its queries: the rows of its visuals, each visual's frame and
    query parts, and the tags of the extra pieces (``companions``: tag -> (visual, piece),
    where a piece is ``total`` or ``highlight``)."""

    ctx: PageContext
    visuals: list[Json]
    framed: dict[str, Framed]
    queries: dict[str, VisualQuery]
    companions: dict[str, tuple[str, str]]
    notes: list[str] = dataclass_field(default_factory=list)

    @property
    def query(self) -> str:
        """The page query: every visual's main piece."""
        return _union([q.main for q in self.queries.values()])

    @property
    def rows_bound(self) -> int:
        return min(5000, len(self.visuals) * 101 + 1)


def _union(pieces: Sequence[str]) -> str:
    if not pieces:
        return ""
    return "EVALUATE " + (f"UNION({','.join(pieces)})" if len(pieces) > 1 else pieces[0])


def _slots(fields: Sequence[Json], piece: str) -> list[tuple[int, Json]]:
    """The fields a piece packs, in slot order. A value needed only for the whole visual (a
    dynamic title's measure) is left out of the grouped piece: it is read from the totals
    piece, so a measure that cannot be computed per row costs nothing there."""
    return [
        (i, f)
        for i, f in enumerate(fields)
        if f["kind"] != "computed" and (piece == "total" or not f.get("_whole"))
    ]


def _compile(
    report: ImportedReport,
    page_id: str,
    request: LiveReportRequest,
    only: set[str] | None = None,
    extra: dict[str, list[str]] | None = None,
) -> Compiled:
    ctx = _page_context(report, page_id, request)
    scope = _scope(ctx, request)
    metadata: list[dict[str, Any]] = []
    queries: dict[str, VisualQuery] = {}
    framed_by_id: dict[str, Framed] = {}
    companions: dict[str, tuple[str, str]] = {}
    ids = {v.id for v in ctx.page.visuals}
    ranks = _ranks(ctx.page)

    def tag(visual_id: str, piece: str) -> str:
        # Unique among the page's visual ids, so a row of a piece is never read as a visual.
        found = f"{visual_id}~{piece}"
        while found in ids or found in companions:
            found += "~"
        companions[found] = (visual_id, piece)
        return found

    for visual in ctx.page.visuals:
        if only is not None and visual.id not in only:
            continue
        framed = _frame(ctx, scope, visual, ranks[visual.id], request)
        item, fields, plan = framed.item, framed.fields, framed.plan
        item["selected"] = scope.selected.get(visual.id, scope.defaults.get(visual.id, []))
        metadata.append(item)
        framed_by_id[visual.id] = framed
        if extra and visual.id in extra and item.get("needs_desktop"):
            # The caller asked for figures under this visual's own filters: an answer
            # computed without them would be wrong, so it gets no answer at all.
            raise Unsupported(str(item["error"]))
        if fields is None or plan is None or "error" in item or item["kind"] not in DATA_KINDS:
            continue
        try:
            main_slots = _slots(fields, "main")
            total_slots = _slots(fields, "total")
            group_refs = list(
                dict.fromkeys(f["ref"] for _, f in main_slots if f["kind"] == "column")
            )
            measures = [(i, f["ref"]) for i, f in main_slots if f["kind"] == "measure"]
            every_measure = [(i, f["ref"]) for i, f in total_slots if f["kind"] == "measure"]
            extras = (extra or {}).get(visual.id, [])
            if extras and (group_refs or only != {visual.id}):
                raise ValueError("Extra figures are computed for one card at a time")
            refs = {i: f["ref"] if f["kind"] == "column" else f"[M{i}]" for i, f in total_slots}
            item["sort"] = plan.sort
            card = item["kind"] == "card"
            base = _grouped(group_refs, measures, extras, plan.filters)
            main = _topn(base, plan.sort, refs, group_refs)
            part = VisualQuery(_packed(main, visual.id, main_slots, refs, card, extras))
            if plan.highlights and group_refs and measures:
                lit_up = _grouped(group_refs, measures, [], plan.filters + plan.highlights)
                part.extras.append(
                    _packed(
                        _topn(lit_up, plan.sort, refs, group_refs),
                        tag(visual.id, "highlight"),
                        main_slots,
                        refs,
                        card,
                        [],
                    )
                )
                part.extra_rows += 101
            grouped_filter = bool(group_refs) and plan.measure_filters
            wanted = _needs_total(item, fields, framed.internals)
            if every_measure and not grouped_filter and wanted:
                total = _grouped([], every_measure, [], plan.filters)
                part.extras.append(
                    _packed(
                        total, tag(visual.id, "total"), total_slots, refs, card, [], totals=True
                    )
                )
                part.extra_rows += 1
            queries[visual.id] = part
        except (Unsupported, KeyError, TypeError, AttributeError) as error:
            _needs_desktop(item, error)
    metadata.sort(key=lambda row: int(row["rank"]))
    return Compiled(ctx, metadata, framed_by_id, queries, companions, list(scope.notes))


def _grouped(
    group_refs: list[str],
    measures: list[tuple[int, str]],
    extras: list[str],
    filters: list[Predicate],
) -> str:
    """The visual's values (grouped by its columns, or one row) under its filters."""
    parts = group_refs + [f'"M{i}", {ref}' for i, ref in measures]
    parts += [f'"X{j}", {dax}' for j, dax in enumerate(extras)]
    base = f"SUMMARIZECOLUMNS({','.join(parts)})" if group_refs else f"ROW({','.join(parts)})"
    for condition, tables, measure in filters:
        if measure:
            base = f"FILTER({base}, {condition})"
        else:
            # _plan has checked that a column filter names exactly one table.
            table = next(iter(tables)).replace("'", "''")
            base = f"CALCULATETABLE({base}, KEEPFILTERS(FILTER(ALL('{table}'), {condition})))"
    return base


def _topn(base: str, sort: list[Json], refs: dict[int, str], group_refs: list[str]) -> str:
    ordering = ",".join(f"{refs[s['index']]}, {'DESC' if s['descending'] else 'ASC'}" for s in sort)
    # Append group keys to break TOPN ties and keep the returned row budget bounded.
    sorted_by = [refs[s["index"]] for s in sort]
    tie_breakers = ",".join(f"{g}, ASC" for g in group_refs if g not in sorted_by)
    return f"TOPN(101, {base}, {ordering}{',' + tie_breakers if tie_breakers else ''})"


def _packed(
    base: str,
    tag: str,
    queried: list[tuple[int, Json]],
    refs: dict[int, str],
    card: bool,
    extras: list[str],
    *,
    totals: bool = False,
) -> str:
    """One piece of the page's UNION: the visual's values as text (C), their type (K) and
    their formatted text (F) in twelve fixed slots, so every piece has the same columns.
    In a ``totals`` piece the columns are blank (there is no category to show)."""
    packed = [f'"Visual", {quoted(tag)}']
    for slot in range(12):
        if slot >= len(queried) or (totals and queried[slot][1]["kind"] == "column"):
            packed.extend([f'"C{slot}", BLANK()', f'"K{slot}", "blank"', f'"F{slot}", BLANK()'])
            continue
        i, field = queried[slot]
        r = refs[i]
        text = (
            f"IF(ISBLANK({r}), BLANK(), IF(ISNUMBER({r}), "
            f'FORMAT({r}, "0.###############", "en-US"), CONVERT({r}, STRING)))'
        )
        kind = (
            f'IF(ISBLANK({r}), "blank", IF(ISNUMBER({r}), "number", '
            f'IF(ISLOGICAL({r}), "boolean", "text")))'
        )
        packed.extend([f'"C{slot}", {text}', f'"K{slot}", {kind}'])
        packed.append(f'"F{slot}", IF(ISBLANK({r}), BLANK(), {_formatted(r, field, card)})')
    packed.extend(f'"X{j}", [X{j}]' for j in range(len(extras)))
    return f"SELECTCOLUMNS({base}, {','.join(packed)})"


# A card's automatic display units (Power BI's en-US suffixes).
_AUTO_UNITS = ((10**9, "bn"), (10**6, "M"), (1000, "K"))


def _formatted(ref: str, field: Json, card: bool) -> str:
    """DAX for a value's formatted text: the field's format string (the model's, or the
    visual's own), with the visual's display units and decimals when it sets them. A card
    whose units are Auto picks K, M or bn by size, as Desktop's callout does."""
    fmt = str(field.get("format") or "")
    units = field.get("displayUnits")
    custom = display_format(fmt, units, field.get("decimals"))
    pattern = custom[1] if custom is not None else fmt
    if custom is not None and (custom[0] != 1 or units == 1 or not card):
        divisor = custom[0]
        scaled = ref if divisor == 1 else f"{ref}/{int(divisor)}"
        return f'FORMAT({scaled}, {quoted(pattern)}, "en-US")'
    formatted = f'FORMAT({ref}, {quoted(pattern)}, "en-US")' if pattern else "BLANK()"
    if card and pattern and not is_percent(pattern) and not field.get("hidden"):
        # The suffix sits inside every section, so a bracketed negative reads ($1.2M).
        automatic = [
            f"ABS({ref}) >= {divisor}, "
            f'FORMAT({ref}/{divisor},{quoted(with_suffix(pattern, suffix))},"en-US")'
            for divisor, suffix in _AUTO_UNITS
        ]
        formatted = f"SWITCH(TRUE(), {','.join(automatic)}, {formatted})"
    return formatted


def render_formatting(
    report: ImportedReport,
    page_id: str,
    request: LiveReportRequest,
    only: set[str] | None = None,
) -> dict[str, Json]:
    """Formatting and captions of a page's visuals with no query: what a Format-pane edit
    changes. ``{visual_id: {...}}`` holds everything a visual row has except its data
    (``DATA_KEYS``): formatting, fidelity, unrendered, title, columns, position, z, rank,
    hidden. A client whose cached ``data_key`` is unchanged replaces those keys in its
    cached row and keeps rows, formatted_rows, selected, truncated and sort.
    """
    ctx = _page_context(report, page_id, request)
    return _formatting_rows(ctx, request, only)[0]


def _formatting_rows(
    ctx: PageContext, request: LiveReportRequest, only: set[str] | None
) -> tuple[dict[str, Json], list[str]]:
    """The formatting rows of the page's visuals and the page notes (section 18)."""
    scope = _scope(ctx, request)
    ranks = _ranks(ctx.page)
    found: dict[str, Json] = {}
    for visual in ctx.page.visuals:
        if only is not None and visual.id not in only:
            continue
        item = _frame(ctx, scope, visual, ranks[visual.id], request).item
        found[visual.id] = {k: v for k, v in item.items() if k not in DATA_KEYS and k != "id"}
    return found, list(scope.notes)


def render_formatting_payload(
    report: ImportedReport,
    page_id: str,
    request: LiveReportRequest,
    only: set[str] | None = None,
    revision: str | None = None,
) -> Json:
    """``render_formatting`` in the envelope the client merges: the page style and theme
    (a page or theme edit changes those, not a visual) and the echoed revision."""
    started = time.monotonic()
    ctx = _page_context(report, page_id, request)
    visuals, notes = _formatting_rows(ctx, request, only)
    return {
        "visuals": visuals,
        **_envelope(ctx, revision, notes),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _envelope(ctx: PageContext, revision: str | None, notes: list[str] | None = None) -> Json:
    """What a page response carries besides its visuals: the canvas, the theme, the echoed
    revision, the page and how view mode navigates it (bookmarks, interactions, synced
    slicers, the phone layout)."""
    archive, blob, page = ctx.archive, ctx.blob, ctx.page
    envelope: Json = {
        # A copy: page_style is cached and the payload belongs to the caller.
        "page_style": copy.deepcopy(page_style(str(archive), blob, page.id)[0]),
        "theme": theme_payload(theme(str(archive), blob)),
        "revision": revision,
        "head_hash": blob,
        "page": {
            "id": page.id,
            "title": page.title,
            "width": page.width,
            "height": page.height,
            "hidden": page.hidden,
            "page_type": page.page_type,
            "binding": nav.page_binding(ctx.page_config),
        },
        "bookmarks": nav.bookmarks_payload(ctx.bookmarks, ctx.pages),
        "bookmark": None,
        "interactions": _interactions(ctx),
        "sync_groups": nav.sync_groups(ctx.pages),
        "mobile": ctx.mobile,
        "drillthroughs": nav.drillthroughs(ctx.pages, ctx.page_configs),
        # Why the page's data visuals are approximate, in plain words (empty: nothing).
        "notes": list(notes or []),
    }
    if ctx.bookmark is not None:
        envelope["bookmark"] = {
            "name": ctx.bookmark.name,
            "displayName": ctx.bookmark.display_name,
            "page": ctx.bookmark.page,
            "captures": ctx.bookmark.captures,
            "notes": list(ctx.bookmark_notes),
        }
    return envelope


def _interactions(ctx: PageContext) -> dict[str, dict[str, str]]:
    """``{source: {target: filter|highlight|none|unsupported}}`` for every visual whose
    selection can reach another on the page (slicers, charts, tables), as ``_plan`` applies
    them."""
    raw = ctx.page_config.get("visualInteractions", [])
    explicit = raw if isinstance(raw, list) else []
    by_default = _filter_by_default(ctx.report_config)
    targets = [v for v in ctx.page.visuals if kind_of(v.visual_type) in DATA_KINDS]
    found: dict[str, dict[str, str]] = {}
    for source in ctx.page.visuals:
        source_kind = kind_of(source.visual_type)
        if source_kind not in SOURCE_KINDS:
            continue
        found[source.id] = {
            target.id: nav.interaction(source, source_kind, target, explicit, by_default)
            for target in targets
            if target.id != source.id
        }
    return found


def _helper_measures(visual_type: str, body: Json) -> Any:
    """What of the formatting changes a visual's rows (see ``data_formatting``).

    A measure named in a switched-off property adds nothing, so toggling ``show`` moves
    the key exactly when it moves the visual's columns. When a projection has no queryRef the
    first field cannot be named without the model, so every object stands in for the helpers.
    """
    refs: list[str] = []
    query = body.get("query")
    state = query.get("queryState") if isinstance(query, dict) else None
    for section in state.values() if isinstance(state, dict) else ():
        projections_ = section.get("projections") if isinstance(section, dict) else None
        for projection in projections_ if isinstance(projections_, list) else ():
            query_ref = projection.get("queryRef") if isinstance(projection, dict) else None
            if not isinstance(query_ref, str):
                return [body.get("objects", {}), data_formatting(visual_type, body, [])]
            refs.append(query_ref)
    return data_formatting(visual_type, body, refs)


def data_key(
    report: ImportedReport,
    visual: ImportedVisual,
    request: LiveReportRequest,
    page_id: str | None = None,
) -> str:
    """A hash of everything that decides a visual's rows and nothing else.

    The client caches a visual's data under this key: it changes with the visual's
    queryState and sort, its own filters, the page and report filters, the interactions
    aimed at it, the measures its formatting adds to the query (only while switched on),
    the slicer state (saved selections, the user's selections and searches) and the data
    model, and it does not change when a title, color, font or position does.
    Formatting-only edits therefore keep the key and ask ``render_formatting`` instead of
    the engine.
    """
    archive, _ = report_archive(report)
    # The head file is the authority: the pages stored on the report can predate an edit.
    pages, _, _ = read_report(archive)
    page = next(
        (
            p
            for p in pages
            if (page_id is None or p.id == page_id) and any(v.id == visual.id for v in p.visuals)
        ),
        None,
    )
    if page is None:
        raise ValueError("This visual does not belong to the imported report")
    with zipfile.ZipFile(archive) as z:
        page_config = json.loads(z.read(f"Report/definition/pages/{page.id}/page.json"))
        report_config = (
            json.loads(z.read("Report/definition/report.json"))
            if "Report/definition/report.json" in z.namelist()
            else {}
        )
        bookmarks = nav.read_bookmarks(z) if request.bookmark else []
    if request.bookmark:
        # A bookmark's slicer and filter states decide the rows as the file's own do.
        applied = _apply_bookmark(
            page, page_config, report_config, nav.find_bookmark(bookmarks, request.bookmark)
        )
        page, page_config, report_config = applied.page, applied.page_config, applied.report_config
    current = next(v for v in page.visuals if v.id == visual.id)
    return _key_of(report, page, current, request, page_config, report_config)


def _key_of(
    report: ImportedReport,
    page: ImportedPage,
    visual: ImportedVisual,
    request: LiveReportRequest,
    page_config: Json,
    report_config: Json,
) -> str:
    """The data key of ``visual``: ``page`` and the configs are as the request draws the
    page (a bookmark applied)."""
    body = visual.definition.get("visual", {})
    slicers = {
        v.id: [
            entry.get("properties", {}).get("filter")
            for entry in v.definition.get("visual", {}).get("objects", {}).get("general", [])
            if isinstance(entry, dict)
        ]
        for v in page.visuals
        if KINDS.get(v.visual_type) == "slicer"
    }
    own_slicer = KINDS.get(visual.visual_type) == "slicer"
    helpers = _helper_measures(visual.visual_type, body)
    palette: list[str] | None = None
    if "ThemeDataColor" in json.dumps(helpers, default=str):
        # A rule or line colored with a theme color: the rows hold the resolved color, so a
        # theme edit must move the key and bring new rows.
        archive, blob = report_archive(report)
        palette = theme_palette(theme(str(archive), blob).get("dataColors", []))
    digest = {
        "model": model_key(report),
        # The tables Power BI draws (known once the model facts are read): a visual using
        # one has no rows here.
        "power_bi_only": sorted(
            (table, kind) for table, (kind, _) in power_bi_only_tables(report).items()
        ),
        "page": page.id,
        "visual": visual.id,
        "type": visual.visual_type,
        "query": body.get("query", {}),
        "filters": visual.definition.get("filterConfig", {}),
        "page_filters": page_config.get("filterConfig", {}),
        "report_filters": report_config.get("filterConfig", {}),
        "interactions": [
            i for i in page_config.get("visualInteractions", []) if i.get("target") == visual.id
        ],
        "helpers": helpers,
        "palette": palette,
        "saved_slicers": slicers,
        "selections": sorted(
            (s.visual_id, [str(v) for v in s.values])
            for s in request.selections
            if not (own_slicer and s.visual_id == visual.id)
        ),
        "search": request.searches.get(visual.id) if own_slicer else None,
        "drill": [(d.field, [str(v) for v in d.values]) for d in request.drill],
        "drill_source": request.drill_source.model_dump() if request.drill_source else None,
        # Whether a selection elsewhere filters or highlights this visual.
        "filter_by_default": _filter_by_default(report_config),
        "highlight": request.highlight,
    }
    encoded = json.dumps(digest, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _order_key(index: int) -> Callable[[tuple[list[Any], list[Any]]], tuple[bool, Any]]:
    def key(record: tuple[list[Any], list[Any]]) -> tuple[bool, Any]:
        cell = record[0][index]
        return (cell is not None, str(cell).casefold() if isinstance(cell, str) else cell or 0)

    return key


def render_page(
    engine: NativeReportPreview | LocalReportEngine,
    report: ImportedReport,
    page_id: str,
    request: LiveReportRequest,
    only: set[str] | None = None,
    extra: dict[str, list[str]] | None = None,
    revision: str | None = None,
) -> dict[str, Any]:
    """The page's visuals with their rows, formatting and fidelity.

    ``revision`` is echoed as ``revision`` (with ``head_hash``) so the client can drop a
    response whose revision is no longer the current head.
    """
    start = time.monotonic()
    compiled = _compile(report, page_id, request, only, extra)
    visuals = compiled.visuals
    by_id = {v["id"]: v for v in visuals}
    model = engine.model if isinstance(engine, LocalReportEngine) else None
    if model is not None:
        _decline_unavailable(compiled, by_id, model)
    query = compiled.query
    rows = engine.query(report, query, compiled.rows_bound).get("rows", []) if query else []
    extra_rows, failed = _extra_rows(engine, report, compiled, model)
    totals: dict[str, tuple[list[Any], list[Any]]] = {}
    highlights: dict[str, list[list[Any]]] = {}
    for row in [*rows, *extra_rows]:
        tag = row.get("[Visual]")
        companion = compiled.companions.get(tag) if isinstance(tag, str) else None
        visual = by_id.get(companion[0] if companion else tag)
        framed = compiled.framed.get(visual["id"]) if visual else None
        if not visual or framed is None or framed.fields is None:
            raise ValueError("The calculation engine returned an unexpected visual")
        piece = companion[1] if companion else "main"
        values, formatted = _decoded(row, framed.fields, piece)
        if piece == "total":
            totals[visual["id"]] = (values, formatted)
            continue
        if piece == "highlight":
            highlights.setdefault(visual["id"], []).append(values)
            continue
        visual["rows"].append(values)
        visual.setdefault("formatted_rows", []).append(formatted)
        if extra and visual["id"] in extra:
            visual["extra"] = [row.get(f"[X{j}]") for j in range(len(extra[visual["id"]]))]
    for visual in visuals:
        visual["truncated"] = len(visual["rows"]) > 100
        # TOPN order is not guaranteed; sort locally using the original sort specification.
        records = list(zip(visual["rows"], visual.get("formatted_rows", []), strict=True))
        for order in reversed(visual.get("sort", [])):
            index = order["index"]
            records.sort(key=_order_key(index), reverse=order["descending"])
        visual["rows"] = [r[0] for r in records[:100]]
        visual["formatted_rows"] = [r[1] for r in records[:100]]
        framed = compiled.framed.get(visual["id"])
        if framed is not None and framed.fields is not None and "error" not in visual:
            lost = visual["id"] in failed
            _finish(
                visual,
                framed,
                None if lost else totals.get(visual["id"]),
                None if lost else highlights.get(visual["id"]),
            )
            if lost:
                _extras_lost(visual, framed)
    return {
        "visuals": visuals,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "engine": getattr(engine, "label", "Power BI Desktop"),
        **_envelope(compiled.ctx, revision, compiled.notes),
    }


# What a query the engine could not answer raises: DaxEngineError, DaxParseError and the
# Desktop engine's refusals are ValueErrors.
_ENGINE_ERRORS = (ValueError, ArithmeticError, LookupError, TypeError)


def _decline_unavailable(compiled: Compiled, by_id: dict[str, Json], model: Any) -> None:
    """Take out of the page query every visual that reads something the local model could
    not build (a calculated column the imported rows lack, a measure Power BI reports an
    error in): it is drawn by Power BI Desktop, with the reason, and the rest of the page
    still renders locally. An extra piece that reads such a field is dropped the same way."""
    for visual_id, part in list(compiled.queries.items()):
        why = unavailable_reference(model, "EVALUATE " + part.main)
        if why is not None:
            del compiled.queries[visual_id]
            item = by_id[visual_id]
            _needs_desktop(item, Unsupported(why))
            # Kept by a client merging a formatting-only row with the same data key.
            item["declined"] = True
            continue
        if part.extras and unavailable_reference(model, _union(part.extras)) is not None:
            part.extras, part.extra_rows = [], 0
            part.lost = True


def _extra_rows(
    engine: NativeReportPreview | LocalReportEngine,
    report: ImportedReport,
    compiled: Compiled,
    model: Any,
) -> tuple[list[Json], set[str]]:
    """The rows of the extra pieces (totals, highlights), asked in a second query after the
    page query. If that query fails, each visual's pieces are asked on their own; a visual
    whose pieces fail is returned in the set (its totals, dynamic values and highlights are
    left out and it is approximate) and the page still renders."""
    lost = {v for v, part in compiled.queries.items() if part.lost}
    pending = {v: part for v, part in compiled.queries.items() if part.extras}
    if not pending:
        return [], lost
    pieces = [piece for part in pending.values() for piece in part.extras]
    bound = min(5000, sum(part.extra_rows for part in pending.values()) + 1)
    try:
        return list(engine.query(report, _union(pieces), bound).get("rows", [])), lost
    except _ENGINE_ERRORS:
        pass
    rows: list[Json] = []
    for visual_id, part in pending.items():
        try:
            found = engine.query(report, _union(part.extras), min(5000, part.extra_rows + 1))
            rows.extend(found.get("rows", []))
        except _ENGINE_ERRORS:
            lost.add(visual_id)
    return rows, lost


def _extras_lost(visual: Json, framed: Framed) -> None:
    """A visual whose totals or highlight piece could not be computed: it keeps its rows and
    says what is not drawn (``unrendered``) and why (``dataNotes``)."""
    paths: list[str] = []
    formatting = visual.get("formatting") or {}
    table = formatting.get("table") if isinstance(formatting, dict) else None
    if isinstance(table, dict) and table.get("total", {}).get("show"):
        paths.append("total.totals")
    dynamic = framed.internals.get("_dynamic")
    paths.extend(sorted(dynamic) if isinstance(dynamic, dict) else [])
    paths.extend(
        f"{spec.object}.value"
        for spec in framed.internals.get("_lines") or []
        if spec.value_index is not None
    )
    visual["unrendered"] = sorted(set(visual.get("unrendered", [])) | set(paths))
    _data_note(
        visual,
        "The totals, highlights or whole-visual values of this visual could not be computed here",
    )


def _data_note(visual: Json, note: str) -> None:
    """Mark a visual approximate for a reason only its data shows (``dataNotes``)."""
    notes = visual.setdefault("dataNotes", [])
    if note not in notes:
        notes.append(note)
    if visual.get("fidelity") == "exact":
        visual["fidelity"] = "approximate"


def _decoded(row: Json, fields: list[Json], piece: str = "main") -> tuple[list[Any], list[Any]]:
    """A result row as the visual's values and formatted texts, in field order. A computed
    field has no slot in the query (``_finish`` fills it), nor has a whole-visual helper in
    a grouped piece."""
    values: list[Any] = [None] * len(fields)
    formatted: list[Any] = [None] * len(fields)
    for slot, (index, _) in enumerate(_slots(fields, piece)):
        value, kind = row.get(f"[C{slot}]"), row.get(f"[K{slot}]")
        if kind == "number" and value is not None:
            value = float(value)
            if not math.isfinite(value):
                value = None
        elif kind == "boolean":
            value = str(value).casefold() == "true"
        elif kind == "blank":
            value = None
        values[index] = value
        formatted[index] = row.get(f"[F{slot}]")
    return values, formatted


_ICON_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")


def _sanitized(cell: Any, what: str) -> Any:
    """A helper measure's value as the formatting may use it: a CSS-safe color, a built-in
    icon name or plain text; None for anything else."""
    if what == "color":
        return safe_color(cell)
    if what == "icon":
        return cell if isinstance(cell, str) and _ICON_NAME.fullmatch(cell) else None
    return clean_text(cell)


def _finish(
    visual: Json,
    framed: Framed,
    total: tuple[list[Any], list[Any]] | None,
    lit_up: list[list[Any]] | None,
) -> None:
    """Complete a visual's rows after the query: sanitize the helper values the formatting
    reads (colors, icons, text), compute the computed columns (visual calculations, then
    conditional formats), and add the totals row, highlights, reference lines, small
    multiples and the values of dynamic titles and backgrounds."""
    fields = framed.fields or []
    rows: list[list[Any]] = visual["rows"]
    image_index = visual.get("formatting", {}).get("imageIndex")
    if image_index is not None and rows:
        visual["formatting"]["image"] = safe_svg(rows[0][image_index])
    for index, field in enumerate(fields):
        what = field.get("_sanitize")
        if isinstance(what, str):
            for row in rows:
                row[index] = _sanitized(row[index], what)
            if total is not None:
                total[0][index] = _sanitized(total[0][index], what)
    # The values for the whole visual (the totals piece): grand totals, dynamic titles.
    whole = total[0] if total is not None else None
    for index, field in enumerate(fields):
        calculation = field.get("_calc")
        rule = field.get("_rule")
        if isinstance(calculation, Calculation):
            grand = whole[calculation.source] if whole is not None else None
            computed: list[Any] = evaluate_calculation(calculation, fields, rows, grand)
        elif rule is not None:
            computed = evaluate(rule, rows)
        else:
            continue
        for row, cell in zip(rows, computed, strict=True):
            row[index] = cell
    formatting = visual.get("formatting") or {}
    if (
        visual.get("truncated")
        and isinstance(formatting, dict)
        and formatting.get("rangeDependent")
    ):
        # A scale, a percent rule, an analytics line or a visual calculation over all the
        # data: only the first 100 rows were computed.
        _data_note(visual, "Only the first 100 rows were used for the values computed here")
    table = formatting.get("table") if isinstance(formatting, dict) else None
    if isinstance(table, dict) and table.get("total", {}).get("show") and total is not None:
        visual["totals"] = {"values": total[0], "formatted": total[1]}
    dynamic = framed.internals.get("_dynamic")
    if isinstance(dynamic, dict) and whole is not None:
        visual["dynamic"] = {
            path: _sanitized(whole[index], "text" if path == "title.text" else "color")
            for path, index in dynamic.items()
        }
    if lit_up is not None:
        # Each row's measures as the selection highlights them (null: nothing of the row
        # is selected), aligned with ``rows``.
        keys = [i for i, f in enumerate(fields) if f["kind"] == "column"]
        shown = {i for i, f in enumerate(fields) if f["kind"] == "measure" and not f.get("hidden")}
        by_key = {tuple(r[i] for i in keys): r for r in lit_up}
        found: list[list[Any]] = []
        for row in rows:
            match = by_key.get(tuple(row[k] for k in keys))
            found.append(
                [
                    row[i] if i in keys else (match[i] if match and i in shown else None)
                    for i in range(len(row))
                ]
            )
        visual["highlights"] = found
    lines = framed.internals.get("_lines")
    if lines:
        visual["referenceLines"] = line_values(lines, fields, rows, whole)
    split = framed.internals.get("_smallMultiples")
    if isinstance(split, int):
        order_by = fields[split].get("_sortBy")
        visual["smallMultiples"] = {
            "index": split,
            "field": fields[split].get("label"),
            **panels(rows, split, order_by if isinstance(order_by, int) else None),
        }


# Stable public names of the query pieces other modules build on (report_tests pins a number
# with the canvas's own filter context; number_explain evaluates a point with the visual's own
# query). They are the renderer's functions themselves, so a test or an explanation can never
# drift from what the canvas computes; the private names stay for older callers.
page_context = _page_context
page_scope = _scope
visual_plan = _plan
compile_visual = _compile
grouped_query = _grouped
unbound_page = _unbound
drill_predicates = _drill_predicates
date_literal = _date_literal
