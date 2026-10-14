"""What chat knows about an imported report when it reads an edit request.

``EditContext`` is a request-scoped index of the report: one row per visual with the text a
person SEES on the canvas (its caption), the fields it shows, where it sits and which hues
it is drawn in. The parser resolves "the top left chart" or "the blue slicer" against it,
never against internal ids. Building it needs no model and no Power BI Desktop.
"""

from __future__ import annotations

import colorsys
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import pbir_expr as expr
from .model_facts import storage_summary
from .report_edit_model import ModelStructure, RelationshipFact
from .report_edits import visible_caption

if TYPE_CHECKING:
    from .report_import import ImportedReport

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")
_GROUP_TYPES = frozenset({"visualGroup", "group"})

# Names a person uses for a visual type. The Power BI Visualizations pane calls
# columnChart "Stacked column chart"; people say "column chart".
TYPE_NAMES: dict[str, str] = {
    "columnChart": "column chart",
    "clusteredColumnChart": "column chart",
    "hundredPercentStackedColumnChart": "column chart",
    "barChart": "bar chart",
    "clusteredBarChart": "bar chart",
    "hundredPercentStackedBarChart": "bar chart",
    "lineChart": "line chart",
    "areaChart": "area chart",
    "stackedAreaChart": "area chart",
    "lineClusteredColumnComboChart": "combo chart",
    "lineStackedColumnComboChart": "combo chart",
    "pieChart": "pie chart",
    "donutChart": "donut chart",
    "treemap": "treemap",
    "funnel": "funnel",
    "waterfallChart": "waterfall chart",
    "ribbonChart": "ribbon chart",
    "scatterChart": "scatter chart",
    "gauge": "gauge",
    "kpi": "KPI",
    "card": "card",
    "cardVisual": "card",
    "multiRowCard": "card",
    "slicer": "slicer",
    "advancedSlicerVisual": "slicer",
    "tableEx": "table",
    "table": "table",
    "pivotTable": "matrix",
    "textbox": "text box",
    "image": "image",
    "shape": "shape",
    "actionButton": "button",
    "pageNavigator": "page navigator",
    "bookmarkNavigator": "bookmark navigator",
    "map": "map",
    "filledMap": "map",
    "azureMap": "map",
    "visualGroup": "group",
}


def type_name(visual_type: str) -> str:
    """A person's word for a visual type ("column chart", "card", "slicer")."""
    known = TYPE_NAMES.get(visual_type)
    if known:
        return known
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", visual_type).casefold().split()
    return " ".join(words) or "visual"


def hue_family(color: str) -> str | None:
    """The hue a person would call this color ("blue", "red", "gray"...), or None."""
    if not _HEX.match(color):
        return None
    red, green, blue = (int(color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, saturation, value = colorsys.rgb_to_hsv(red, green, blue)
    if value < 0.16:
        return "black"
    if saturation < 0.12:
        return "white" if value > 0.9 else "gray"
    degrees = hue * 360
    if 10 <= degrees < 45 and value < 0.62:
        return "brown"
    if degrees < 12 or degrees >= 345:
        return "red"
    if degrees < 42:
        return "orange"
    if degrees < 68:
        return "yellow"
    if degrees < 165:
        return "green"
    if degrees < 195:
        return "teal"
    if degrees < 258:
        return "blue"
    if degrees < 300:
        return "purple"
    return "pink"


def _literal_hexes(node: Any, found: list[str]) -> None:
    """Every #RRGGBB text literal in an object tree, in file order."""
    if isinstance(node, dict):
        value = node.get("Value") if "Value" in node and len(node) == 1 else None
        if isinstance(value, str):
            text = value.strip("'")
            if _HEX.match(text):
                found.append(text.upper())
        for child in node.values():
            _literal_hexes(child, found)
    elif isinstance(node, list):
        for child in node:
            _literal_hexes(child, found)


def literal_hexes(node: Any) -> list[str]:
    """Every #RRGGBB text literal anywhere in a piece of PBIR JSON, in file order."""
    found: list[str] = []
    _literal_hexes(node, found)
    return found


# A visual is "the blue chart" by what it is filled with, not by its text: only these
# properties count (a chart's bars are its dataPoint fills; text colors say nothing).
_FILL_PROPERTIES = frozenset({"fill", "fillColor", "backgroundColor", "background"})


def _fill_hexes(objects: Mapping[str, Any], found: list[str]) -> None:
    for entries in objects.values():
        for entry in entries if isinstance(entries, list) else []:
            properties = entry.get("properties", {}) if isinstance(entry, dict) else {}
            for name, value in properties.items():
                if name in _FILL_PROPERTIES:
                    _literal_hexes(value, found)


def visual_colors(definition: Mapping[str, Any]) -> list[str]:
    """The literal hex colors a visual is filled with: its data points first, then its other
    fills and the background of its box."""
    body = definition.get("visual", {})
    objects = body.get("objects", {})
    found: list[str] = []
    _literal_hexes(objects.get("dataPoint", []), found)
    _fill_hexes({k: v for k, v in objects.items() if k != "dataPoint"}, found)
    _literal_hexes(body.get("visualContainerObjects", {}).get("background", []), found)
    return list(dict.fromkeys(found))


def categories_with_colors(definition: Mapping[str, Any]) -> list[str]:
    """Category values that already have a color of their own (TopN, BottomN, Others)."""
    values: list[str] = []
    for entry in definition.get("visual", {}).get("objects", {}).get("dataPoint", []):
        for item in entry.get("selector", {}).get("data", []):
            literal = (
                item.get("scopeId", {}).get("Comparison", {}).get("Right", {}).get("Literal", {})
            )
            text = literal.get("Value") if isinstance(literal, dict) else None
            if isinstance(text, str):
                decoded = expr.decode({"expr": {"Literal": {"Value": text}}})
                if isinstance(decoded, str):
                    values.append(decoded)
    return list(dict.fromkeys(values))


def _hexes_from(colors: object) -> list[str]:
    """Colors from ImportedVisual.colors, whatever shape that field ends up with."""
    found: list[str] = []
    if isinstance(colors, str):
        found.append(colors)
    elif isinstance(colors, Mapping):
        for key, value in colors.items():
            found.extend(_hexes_from(value))
            if isinstance(key, str):
                found.extend(_hexes_from(key))
    elif isinstance(colors, list | tuple | set | frozenset):
        for item in colors:
            found.extend(_hexes_from(item))
    return [c.upper() for c in found if isinstance(c, str) and _HEX.match(c)]


def _bindings(definition: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """(kind, table, name) of every field in a visual's field wells, in role order."""
    rows: list[tuple[str, str, str]] = []
    state = definition.get("visual", {}).get("query", {}).get("queryState", {})
    for bucket in state.values():
        for projection in bucket.get("projections", []):
            node = projection.get("field", {})
            for kind in ("Measure", "Column"):
                found = node.get(kind)
                if isinstance(found, dict):
                    ref = found.get("Expression", {}).get("SourceRef", {})
                    entity = str(ref.get("Entity", ref.get("Source", "")))
                    rows.append((kind.lower(), entity, str(found.get("Property", ""))))
                    break
            else:
                column = node.get("Aggregation", {}).get("Expression", {}).get("Column")
                if isinstance(column, dict):
                    ref = column.get("Expression", {}).get("SourceRef", {})
                    rows.append(
                        ("measure", str(ref.get("Entity", "")), str(column.get("Property", "")))
                    )
    return tuple(rows)


def _display_field_names(definition: Mapping[str, Any]) -> tuple[str, ...]:
    """The names shown for a projection, without reading any calculation body.

    Desktop keeps a renamed header in displayName/nativeQueryRef while its field still
    names the canonical model object. Visual calculations also have a local Name.
    """
    names: list[str] = []
    state = definition.get("visual", {}).get("query", {}).get("queryState", {})
    for bucket in state.values():
        for projection in bucket.get("projections", []):
            candidates = [projection.get("displayName"), projection.get("nativeQueryRef")]
            calculation = projection.get("field", {}).get("NativeVisualCalculation")
            if isinstance(calculation, dict):
                candidates.append(calculation.get("Name"))
            for value in candidates:
                if isinstance(value, str) and value.strip():
                    names.append(value.strip())
    return tuple(dict.fromkeys(names))


# --- the index -------------------------------------------------------------------------


@dataclass(frozen=True)
class VisualEntry:
    """One row of the request-scoped index."""

    id: str
    page_id: str
    page_name: str
    visual_type: str
    # The text a user SEES (card label, slicer header, chart title, text box text), never
    # the internal id. Empty when the visual shows none.
    caption: str
    fields: tuple[str, ...]  # displayed aliases and canonical names of the bound fields
    x: float
    y: float
    width: float
    height: float
    z: int | None = None
    hidden: bool = False
    group: str | None = None
    hues: tuple[str, ...] = ()  # hue families of its effective colors
    # Not part of the documented row, added for the resolver and op builders:
    categories: tuple[str, ...] = ()  # category values with a color of their own
    bindings: tuple[tuple[str, str, str], ...] = ()  # (kind, table, name) per field
    definition: dict[str, Any] | None = field(default=None, repr=False, compare=False, hash=False)

    @property
    def is_group(self) -> bool:
        return self.visual_type in _GROUP_TYPES

    @property
    def cx(self) -> float:
        return self.x + self.width / 2

    @property
    def cy(self) -> float:
        return self.y + self.height / 2

    @property
    def label(self) -> str:
        """How a receipt names it: its caption, else what it shows."""
        if self.caption:
            return f"'{self.caption}'"
        kind = type_name(self.visual_type)
        if self.fields:
            return f"the {self.fields[0]} {kind}"
        return f"the {kind}"


@dataclass(frozen=True)
class PageEntry:
    id: str
    name: str
    index: int
    width: float
    height: float
    hidden: bool = False


@dataclass(frozen=True)
class MeasureEntry:
    name: str
    table: str
    expression: str
    format_string: str | None = None


@dataclass(frozen=True)
class LastEdit:
    """The newest change in the report's history, for "undo the color change"."""

    revision_id: str
    ops: tuple[dict[str, Any], ...] = ()
    summary: str = ""
    author: str = "studio"

    @property
    def is_color_edit(self) -> bool:
        return any(op_changes_color(op) for op in self.ops)


# The colors an operation gives (Studio changes no styling: these are a shape's, a conditional
# format's and an analytics line's own colors).
_COLOR_FIELDS = {
    "add_shape": ("fill", "outline"),
    "add_textbox": ("color",),
    "set_conditional_format": (
        "min_color",
        "mid_color",
        "max_color",
        "blank_color",
        "positive_color",
        "negative_color",
        "axis_color",
        "rules",
    ),
    "set_analytics_line": ("color", "label_color", "shade_color", "upper_color", "lower_color"),
}


def op_changes_color(op: Mapping[str, Any]) -> bool:
    return any(op.get(key) is not None for key in _COLOR_FIELDS.get(str(op.get("op")), ()))


_LAYOUT_OPS = frozenset({"align_visuals", "distribute_visuals", "swap_visuals", "place_visual"})
_FILTER_OPS = frozenset(
    {"set_page_filter", "set_visual_filter", "set_report_filter", "clear_filter"}
)


def change_kinds(made: Iterable[Mapping[str, Any]]) -> set[str]:
    """What kinds of change operations made, in the words a person uses to undo one: "color",
    "size", "layout", "delete", "rename", "hide", "page", "measure", "filter", "add"."""
    kinds: set[str] = set()
    for op in made:
        name = str(op.get("op"))
        if op_changes_color(op):
            kinds.add("color")
        if name == "set_position":
            kinds.add("layout")
            if op.get("width") is not None or op.get("height") is not None:
                kinds.add("size")
        elif name in _LAYOUT_OPS:
            kinds.add("layout")
        elif name in _FILTER_OPS:
            kinds.add("filter")
        elif name in {"add_textbox", "add_shape"}:
            kinds.add("add")
        elif name in {"delete_visual", "delete_page"}:
            kinds.add("delete")
        elif name in {"rename_page", "rename_measure"}:
            kinds.add("rename")
        elif name in {"set_visual_hidden", "set_page_hidden"}:
            kinds.add("hide")
        elif name == "arrange":
            kinds.add("layout")
        if name.endswith("_page") or name == "set_page_size":
            kinds.add("page")
        if name.endswith("_measure"):
            kinds.add("measure")
    return kinds


@dataclass
class EditContext:
    pages: list[PageEntry]
    visuals: list[VisualEntry]
    measures: list[MeasureEntry]
    tables: dict[str, list[str]]  # table -> column names
    selection: str | None = None  # visual id selected on the canvas (or attached chip)
    active_page: str | None = None
    head_revision: str | None = None
    last_edit: LastEdit | None = None
    palette: dict[str, str] = field(default_factory=dict)  # theme color name -> hex
    column_types: dict[str, str] = field(default_factory=dict)  # "Table.Column" -> dtype
    relationships: tuple[RelationshipFact, ...] = ()
    relationships_known: bool = False
    storage_modes: dict[str, str] = field(default_factory=dict)
    model_settings: dict[str, str | bool | int] = field(default_factory=dict)
    model_revision: str | None = None

    def page(self, page_id: str) -> PageEntry | None:
        return next((p for p in self.pages if p.id == page_id), None)

    def visual(self, visual_id: str) -> VisualEntry | None:
        return next((v for v in self.visuals if v.id == visual_id), None)

    def visuals_on(self, page_id: str) -> list[VisualEntry]:
        return [v for v in self.visuals if v.page_id == page_id]

    def measure(self, name: str) -> MeasureEntry | None:
        wanted = name.casefold()
        return next((m for m in self.measures if m.name.casefold() == wanted), None)

    @classmethod
    def from_report(
        cls,
        report: ImportedReport,
        *,
        selection: str | None = None,
        active_page: str | None = None,
        head_revision: str | None = None,
        last_edit: LastEdit | None = None,
        palette: Mapping[str, str] | None = None,
    ) -> EditContext:
        pages: list[PageEntry] = []
        visuals: list[VisualEntry] = []
        for index, page in enumerate(report.pages):
            pages.append(
                PageEntry(
                    id=page.id,
                    name=page.title,
                    index=index,
                    width=float(page.width),
                    height=float(page.height),
                )
            )
            for item in page.visuals:
                visuals.append(_entry(item, page.id, page.title))
        measures = [
            MeasureEntry(
                name=str(m["Name"]),
                table=str(m.get("TableName", "")),
                expression=str(m.get("Expression") or ""),
                format_string=(str(m["FormatString"]) if m.get("FormatString") else None),
            )
            for m in report.measures
            if m.get("Name")
        ]
        tables: dict[str, list[str]] = {}
        column_types: dict[str, str] = {}
        for row in report.tables:
            table, column = str(row.get("TableName", "")), str(row.get("ColumnName", ""))
            if table and column:
                tables.setdefault(table, []).append(column)
                column_types[f"{table}.{column}"] = str(row.get("PandasDataType", ""))
        facts = getattr(report, "model", None)
        structure = ModelStructure.from_facts(facts)
        cuts = facts.get("truncated", []) if isinstance(facts, Mapping) else []
        modes = storage_summary(facts)["tables"]
        settings = {
            key: value
            for key, value in structure.settings.items()
            if key in {"defaultMode", "discourageImplicitMeasures", "culture", "collation"}
            and isinstance(value, str | bool | int)
        }
        return cls(
            pages=pages,
            visuals=visuals,
            measures=measures,
            tables=tables,
            selection=selection,
            active_page=active_page,
            head_revision=head_revision,
            last_edit=last_edit,
            palette=dict(palette or {}),
            column_types=column_types,
            relationships=tuple(structure.relationships[:256]),
            relationships_known=(
                "relationships" in structure.known
                and "relationships" not in cuts
                and len(structure.relationships) <= 256
            ),
            storage_modes={row["name"]: row["mode"] for row in modes[:256]},
            model_settings=settings,
            model_revision=(
                head_revision
                or getattr(report, "revision_hash", None)
                or getattr(report, "source_hash", None)
            ),
        )


def _entry(item: Any, page_id: str, page_name: str) -> VisualEntry:
    definition: dict[str, Any] = item.definition or {}
    position = definition.get("position", {})
    bindings = _bindings(definition)
    caption = visible_caption(definition) or ""
    if not caption and item.visual_type in {"slicer", "advancedSlicerVisual"} and bindings:
        # A slicer with no header text shows its field's name.
        caption = bindings[0][2]
    z = getattr(item, "z", None)
    if z is None and position.get("z") is not None:
        z = int(position["z"])
    hidden = getattr(item, "hidden", None)
    group = getattr(item, "parent_group", None) or definition.get("parentGroupName")
    hexes = _hexes_from(getattr(item, "colors", None)) or visual_colors(definition)
    hues = tuple(dict.fromkeys(h for h in (hue_family(c) for c in hexes) if h))
    names = tuple(
        dict.fromkeys(
            (
                *_display_field_names(definition),
                *(b["name"] for b in item.bindings if b.get("name")),
                *(b[2] for b in bindings if b[2]),
            )
        )
    )
    return VisualEntry(
        id=item.id,
        page_id=page_id,
        page_name=page_name,
        visual_type=item.visual_type,
        caption=caption,
        fields=names,
        x=float(item.x),
        y=float(item.y),
        width=float(item.width),
        height=float(item.height),
        z=z,
        hidden=bool(hidden) or bool(definition.get("isHidden")),
        group=group,
        hues=hues,
        categories=tuple(categories_with_colors(definition)),
        bindings=bindings,
        definition=definition,
    )


def describe_position(entry: VisualEntry, page: PageEntry | None) -> str:
    """Where a visual sits on its page: "top left", "middle", "bottom right"."""
    width = page.width if page else max(entry.x + entry.width, 1)
    height = page.height if page else max(entry.y + entry.height, 1)
    column = "left" if entry.cx / width < 0.34 else "right" if entry.cx / width > 0.66 else ""
    row = "top" if entry.cy / height < 0.34 else "bottom" if entry.cy / height > 0.66 else ""
    both = " ".join(part for part in (row, column) if part)
    return both or "middle"
