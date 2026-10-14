"""What every operation family shares: the model facts, the outcome, and small visual helpers.

- ``ModelInfo`` is the imported model's tables, columns and measures the operations check
  against, with what the model's metadata adds when the caller has it (data types, default
  summarization, hidden flags, the DAX of measures and calculated tables, field parameters);
  ``EditOutcome`` is what a batch reports back.
- The helpers read and write pieces of a visual's PBIR JSON: its type, query state,
  projections, formatting objects, selectors and the caption a user sees. A family imports
  them from here and never from another family, which keeps the families independent.

Review items expected to land here: P25 (match fields by identity, not by display name, in
``_projections`` users), P14 (open selector records in the selector helpers) and P15 (one
caption map behind ``visible_caption``). Keep everything here pure: no I/O, no handlers.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from . import pbir_expr as expr
from .pbir_document import DocumentError, PageDoc, PbirDocument
from .report_captions import DYNAMIC_TITLE
from .report_edit_ops import FieldRef
from .report_refs import _tokenize
from .report_roles import projection_label, visual_type_name

# --- model facts the operations check against -----------------------------------------


class FieldLike(Protocol):
    """Anything that names a field: the operations' ``FieldRef`` (table required) and the filter
    module's (table optional)."""

    @property
    def table(self) -> str | None: ...

    @property
    def name(self) -> str: ...


@dataclass(frozen=True)
class FieldFacts:
    """What the model's metadata says about one column or measure (``pbix_extract.types``)."""

    kind: Literal["column", "measure"]
    data_type: int | str | None = None  # the model's DataType id (2 text, 6 whole number...)
    summarize_by: str | None = None  # "sum", "none", "default"...; columns only
    hidden: bool = False


def _key(table: str, name: str) -> tuple[str, str]:
    return table.casefold(), name.casefold()


@dataclass
class ModelInfo:
    """Tables, columns (with pandas dtypes) and measures of the imported model.

    ``columns`` and ``measures`` are what the handlers check names against (and, for measures,
    update as they go). The rest is optional knowledge that sharpens the checks and is simply
    absent when the caller has none: ``facts`` (data types, summarization, hidden flags from
    the model's metadata), ``measure_expressions`` (measure name -> DAX) and ``calculated``
    (``Table[Column]`` or ``Table`` -> DAX of calculated columns and tables). Code that builds
    a ``ModelInfo`` by hand with just ``columns`` and ``measures`` keeps working.
    """

    columns: dict[str, dict[str, str]] = field(default_factory=dict[str, Any])
    measures: dict[str, str] = field(default_factory=dict[str, Any])  # measure -> home table
    facts: dict[tuple[str, str], FieldFacts] = field(
        default_factory=dict[tuple[str, str], FieldFacts]
    )
    measure_expressions: dict[str, str] = field(default_factory=dict[str, Any])
    calculated: dict[str, str] = field(default_factory=dict[str, Any])

    @classmethod
    def from_report(
        cls,
        tables: list[dict[str, Any]],
        measures: list[dict[str, Any]],
        types: Mapping[str, Any] | None = None,
        *,
        calculated: Mapping[str, str] | None = None,
    ) -> ModelInfo:
        """The model of an imported report.

        ``tables`` and ``measures`` are the import's rows (``TableName``, ``ColumnName``,
        ``PandasDataType``; ``Name``, ``TableName``, ``Expression``). ``types`` is the optional
        ``pbix_extract.types`` payload, ``{"columns": [...], "measures": [...]}``."""
        info = cls()
        for row in tables:
            info.columns.setdefault(str(row.get("TableName")), {})[str(row.get("ColumnName"))] = (
                str(row.get("PandasDataType", ""))
            )
        for row in measures:
            name = str(row.get("Name"))
            info.measures[name] = str(row.get("TableName"))
            expression = row.get("Expression")
            if isinstance(expression, str):
                info.measure_expressions[name] = expression
        if calculated:
            info.calculated.update(calculated)
        if types:
            info._read_types(types)
        return info

    def _read_types(self, types: Mapping[str, Any]) -> None:
        for kind, rows in (("column", types.get("columns")), ("measure", types.get("measures"))):
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or not row.get("table") or not row.get("name"):
                    continue
                data_type = row.get("dataType")
                summarize = row.get("summarizeBy")
                self.facts[_key(str(row["table"]), str(row["name"]))] = FieldFacts(
                    "column" if kind == "column" else "measure",
                    data_type if isinstance(data_type, int | str) else None,
                    summarize if isinstance(summarize, str) and summarize else None,
                    bool(row.get("hidden")),
                )

    def with_measures(self, measures: dict[str, str]) -> ModelInfo:
        """A copy with another measure map and everything else kept: the model as it was before
        (or will be after) measure operations, with the columns' types intact."""
        kept = {m.casefold() for m in measures}
        return ModelInfo(
            columns={table: dict(cols) for table, cols in self.columns.items()},
            measures=dict(measures),
            facts={k: v for k, v in self.facts.items() if v.kind == "column" or k[1] in kept},
            measure_expressions={
                m: e for m, e in self.measure_expressions.items() if m.casefold() in kept
            },
            calculated=dict(self.calculated),
        )

    def table(self, name: str) -> str | None:
        """A table's own spelling; measure-only tables (# Measures) count too."""
        known = {t.casefold(): t for t in [*self.columns, *self.measures.values()]}
        return known.get(name.casefold())

    def measure_table(self, name: str) -> str | None:
        lowered = {m.casefold(): m for m in self.measures}
        found = lowered.get(name.casefold())
        return self.measures[found] if found else None

    def measure_name(self, name: str) -> str | None:
        """A measure's own spelling."""
        return next((m for m in self.measures if m.casefold() == name.casefold()), None)

    def resolve(self, ref: FieldLike) -> tuple[str, str, str]:
        """(kind, table, name) with the model's own spelling, or DocumentError.

        Without a table (``ref.table`` is None) the one column or measure with that name is
        found; several, or none, is an error that names the candidates."""
        if ref.table is None:
            return self._resolve_bare(ref.name)
        measure_home = self.measure_table(ref.name)
        tables = {t.casefold(): t for t in self.columns}
        table = tables.get(ref.table.casefold())
        if measure_home and (table is None or table.casefold() == measure_home.casefold()):
            name = next(m for m in self.measures if m.casefold() == ref.name.casefold())
            return "measure", measure_home, name
        if table is None:
            # A table that only holds measures (# Measures) exists, it just has no such measure.
            holder = self.table(ref.table)
            if holder is not None:
                raise DocumentError(f"{holder} has no column or measure named {ref.name!r}")
            raise DocumentError(f"The model has no table {ref.table!r}")
        columns = {c.casefold(): c for c in self.columns[table]}
        column = columns.get(ref.name.casefold())
        if column is None:
            raise DocumentError(f"{table} has no column or measure named {ref.name!r}")
        return "column", table, column

    def _resolve_bare(self, name: str) -> tuple[str, str, str]:
        wanted = name.casefold()
        found: list[tuple[str, str, str]] = []
        measure = self.measure_name(name)
        if measure is not None:
            found.append(("measure", self.measures[measure], measure))
        for table, columns in self.columns.items():
            found.extend(("column", table, c) for c in columns if c.casefold() == wanted)
        if not found:
            raise DocumentError(f"The model has no column or measure named {name!r}")
        if len(found) > 1:
            options = ", ".join(
                f"the measure [{n}]" if kind == "measure" else f"the column {t}[{n}]"
                for kind, t, n in found[:4]
            )
            raise DocumentError(
                f"{name!r} could mean {options}"
                + (" and more" if len(found) > 4 else "")
                + "; say which table it is in"
            )
        return found[0]

    # --- what the metadata says --------------------------------------------------------------

    def _facts(self, table: str, name: str) -> FieldFacts | None:
        return self.facts.get(_key(table, name))

    def data_type(self, table: str, name: str) -> str | int | None:
        """The model's data type id (2 text, 6 whole number, 8 float, 9 date/time, 10 fixed
        decimal, 11 true/false) when the metadata gave one, else the pandas dtype the import
        recorded, else None (a measure the model does not type)."""
        facts = self._facts(table, name)
        if facts is not None and facts.data_type is not None:
            return facts.data_type
        home = self.measure_table(name)
        if home is not None and home.casefold() == table.casefold():
            return None
        for known, columns in self.columns.items():
            if known.casefold() == table.casefold():
                for column, dtype in columns.items():
                    if column.casefold() == name.casefold():
                        return dtype or None
        return None

    def summarize_by(self, table: str, name: str) -> str | None:
        """The column's default summarization ("sum", "none"...), None when not known."""
        facts = self._facts(table, name)
        return facts.summarize_by if facts is not None else None

    def is_hidden(self, table: str, name: str) -> bool:
        facts = self._facts(table, name)
        return facts.hidden if facts is not None else False

    def field_parameter_tables(self) -> set[str]:
        """Tables whose DAX uses NAMEOF: Power BI's field parameters. Known only from the
        calculated tables the model was given, so empty without them."""
        found: set[str] = set()
        for key, expression in self.calculated.items():
            if key.rstrip().endswith("]"):
                continue  # a calculated column
            if any(t.kind == "word" and t.text.upper() == "NAMEOF" for t in _tokenize(expression)):
                found.add(key)
        return found

    def is_numeric(self, table: str, column: str) -> bool:
        facts = self._facts(table, column)
        if facts is not None and facts.kind == "column":
            named = expr.data_type_name(facts.data_type)
            if named is not None:
                return named in {"integer", "double", "decimal"}
        dtype = self.columns.get(table, {}).get(column, "").casefold()
        return any(t in dtype for t in ("int", "float", "decimal", "number"))


# --- results --------------------------------------------------------------------------


@dataclass(frozen=True)
class Protected:
    """Formatting an edit left alone because overwriting it would lose something (a
    conditional-format measure): ``what`` says which, in plain English."""

    visual: str
    what: str


@dataclass(frozen=True)
class VisibleIn:
    """Where a written property shows: Studio's canvas, Power BI only (Studio does not draw
    it), or hidden under an opaque page background image."""

    op: str
    where: Literal["studio", "powerbi_only", "covered_by_background"]
    detail: str = ""


@dataclass(frozen=True)
class Choice:
    """One answer to a question the user has to settle (``NeedsChoice``). ``fill`` carries the
    option values to re-apply the operation with (for example ``{"overrides": "clear"}``)."""

    label: str
    fill: dict[str, Any] = field(default_factory=dict[str, Any])


class NeedsConfirmation(DocumentError):
    """The edit would lose something the user may want (conditional formatting, references
    to a page) and needs an explicit yes. Nothing was written. ``kind`` names the flag that
    confirms it (``replace_conditional``, ``remove_page_references``...)."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail
        self.step: int | None = None


class NeedsChoice(DocumentError):
    """The request is ambiguous or has several reasonable readings: ``options`` are the
    answers to offer. Nothing was written."""

    def __init__(self, question: str, options: list[Choice]) -> None:
        super().__init__(question)
        self.question = question
        self.options = options
        self.step: int | None = None


@dataclass
class HandlerResult:
    """What a handler reports beyond its one-line summary."""

    summary: str
    warnings: list[str] = field(default_factory=list[str])
    protected: list[Protected] = field(default_factory=list[Protected])
    replaced: list[str] = field(default_factory=list[str])  # "TopN blue, BottomN red"
    visible_in: list[VisibleIn] = field(default_factory=list[VisibleIn])
    # Model operations the handler needs applied after the report change (for example the
    # dependents of a renamed measure) and ones it only suggests (colors inside DAX): the
    # first run through Desktop with the batch, the second are shown to the user, never applied.
    model_operations: list[Any] = field(default_factory=list[Any])
    pending_model_operations: list[Any] = field(default_factory=list[Any])
    # True when the handler did only part of what was asked, by design (it left something
    # alone and says so in ``warnings``): the reply then says "partly", never "done".
    partial: bool = False
    # Visuals and pages the handler copied: new name -> the name of its source. A copy starts
    # with the source's defects, so validation holds it to the source's baseline.
    lineage: dict[str, str] = field(default_factory=dict[str, str])


@dataclass
class EditOutcome:
    summaries: list[str] = field(default_factory=list)
    model_operations: list[Any] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    report_changed: bool = False
    protected: list[Protected] = field(default_factory=list[Protected])
    replaced: list[str] = field(default_factory=list[str])
    visible_in: list[VisibleIn] = field(default_factory=list[VisibleIn])
    pending_model_operations: list[Any] = field(default_factory=list[Any])
    # Some operation was skipped in part by design (see ``HandlerResult.partial``).
    partial: bool = False
    # Every copy the batch made (see ``HandlerResult.lineage``), for ``validate_findings``.
    lineage: dict[str, str] = field(default_factory=dict[str, str])


def page_copy_lineage(
    document: PbirDocument, source: PageDoc, pages_before: Collection[str]
) -> dict[str, str]:
    """Copy -> source names for a page the handler duplicated, for a handler that reports none:
    the one page that is new, and its visuals paired with the source's in order (a page copy
    keeps the source's visual order). Empty when the pairing is not clear."""
    fresh = [name for name in document.pages if name not in pages_before]
    if len(fresh) != 1:
        return {}
    copy_page = document.pages[fresh[0]]
    lineage = {copy_page.name: source.name}
    if len(copy_page.visuals) == len(source.visuals):
        lineage.update(zip(copy_page.visuals, source.visuals, strict=True))
    return lineage


# A handler applies one operation to the document and returns its plain-English summary, or a
# ``HandlerResult`` when it has more to say. The operation is typed ``Any`` here because one
# table holds handlers of every class.
Handler = Callable[[PbirDocument, Any, ModelInfo], str | HandlerResult]


# --- helpers ----------------------------------------------------------------------------


def visible_caption(visual: dict[str, Any]) -> str | None:
    """The caption a user sees on the canvas: a new card's label text, a slicer's
    header, a text box's text, else the container title when it is shown."""
    body = visual.get("visual", {})
    objects, container = body.get("objects", {}), body.get("visualContainerObjects", {})

    def text_of(entries: list[dict[str, Any]]) -> str | None:
        shown = True
        found = None
        for entry in entries:
            props = entry.get("properties", {})
            if expr.decode(props.get("show")) is False:
                shown = False
            text = expr.decode(props.get("text"))
            if isinstance(text, str) and text.strip():
                found = text.strip()
        return found if shown else None

    visual_type = body.get("visualType")
    if visual_type == "cardVisual":
        caption = text_of(objects.get("label", []))
        if caption:
            return caption
    if visual_type == "slicer":
        caption = text_of(objects.get("header", []))
        if caption:
            return caption
    if visual_type == "textbox":
        paragraphs = objects.get("general", [{}])[0].get("properties", {}).get("paragraphs", [])
        runs = [
            str(run.get("value", ""))
            for paragraph in paragraphs
            if isinstance(paragraph, dict)
            for run in paragraph.get("textRuns", [])
            if isinstance(run, dict)
        ]
        if "".join(runs).strip():
            return " ".join(" ".join(runs).split())[:120]
    return text_of(container.get("title", []))


def _type_word(visual: dict[str, Any]) -> str:
    """What Power BI calls the visual's type: "table", "clustered column chart", "card"."""
    kind = _visual_type(visual) or "visual"
    known = visual_type_name(kind)
    return known if known != kind else re.sub(r"(?<!^)(?=[A-Z])", " ", kind).lower()


def _label(visual: dict[str, Any], fallback: str) -> str:
    """The visual as a person points at it, for a message: its caption when the canvas shows
    one ('Sales by channel'), else its type and first field ('the table of Agent ID'), else its
    type and place. A raw type such as tableEx or an internal id is never shown while there is a
    caption or a field; ``fallback`` (a short id) is the last resort, when the visual has no
    position either."""
    group = visual.get("visualGroup")
    if isinstance(group, dict):
        name = " ".join(str(group.get("displayName") or "").split())
        return f"the group '{name}'" if name else "a group of visuals"
    caption = visible_caption(visual)
    if caption and caption != DYNAMIC_TITLE:
        caption = " ".join(caption.split())
        return f"'{caption if len(caption) <= 60 else caption[:59].rstrip() + '…'}'"
    word = _type_word(visual)
    first = next(iter(_projections(visual)), None)
    if first:
        return f"the {word} of {projection_label(first)}"
    position = visual.get("position")
    if isinstance(position, dict) and not visual.get("parentGroupName"):
        x, y = position.get("x"), position.get("y")
        if isinstance(x, int | float) and isinstance(y, int | float):
            return f"the {word} at {round(x)}, {round(y)}"
    return f"the {word} {fallback}"


def _visual_type(visual: dict[str, Any]) -> str:
    return str(visual.get("visual", {}).get("visualType", ""))


def _query_state(visual: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = (
        visual.setdefault("visual", {}).setdefault("query", {}).setdefault("queryState", {})
    )
    return state


def _projections(visual: dict[str, Any]) -> list[dict[str, Any]]:
    state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    return [p for role in state.values() for p in role.get("projections", [])]


def _objects(visual: dict[str, Any], where: str) -> dict[str, Any]:
    objects: dict[str, Any] = visual.setdefault("visual", {}).setdefault(where, {})
    return objects


def _set(
    document: PbirDocument,
    visual: dict[str, Any],
    object_name: str,
    prop: str,
    value: Any,
    selector: dict[str, Any] | None = None,
) -> None:
    visual_type = _visual_type(visual)
    spec = expr.property_spec(visual_type, object_name, prop)
    if spec is None:
        raise DocumentError(
            f"A {visual_type} visual has no {object_name}.{prop} setting in Power BI"
        )
    where, definition = spec
    encoded = expr.encode_property(definition, prop, value)
    # The visual type lets the write go under {"id": "default"} where the type only honors
    # formatting there (a shape's fill, a card's value): without it the entry is ignored.
    if selector is None:
        expr.set_everywhere(
            _objects(visual, where), object_name, prop, encoded, visual_type=visual_type
        )
    else:
        expr.set_property(
            _objects(visual, where), object_name, prop, encoded, selector, visual_type=visual_type
        )


def _has(visual: dict[str, Any], object_name: str, prop: str) -> bool:
    return expr.property_spec(_visual_type(visual), object_name, prop) is not None


def _series_selector(visual: dict[str, Any], series: str) -> dict[str, Any]:
    wanted = series.casefold()
    for projection in _projections(visual):
        names = {str(projection.get(k, "")).casefold() for k in ("queryRef", "nativeQueryRef")}
        prop = str(
            projection.get("field", {}).get("Measure", {}).get("Property")
            or projection.get("field", {}).get("Column", {}).get("Property")
            or ""
        ).casefold()
        if wanted in names or wanted == prop:
            return expr.metadata_selector(projection["queryRef"])
    raise DocumentError(f"This visual has no field {series!r}")


def _category_selector(visual: dict[str, Any], value: str | int | float) -> dict[str, Any]:
    state = visual.get("visual", {}).get("query", {}).get("queryState", {})
    for role in ("Category", "Rows", "Series", "Legend"):
        for projection in state.get(role, {}).get("projections", []):
            column = projection.get("field", {}).get("Column")
            if column:
                entity = column["Expression"]["SourceRef"]["Entity"]
                return expr.category_selector(entity, column["Property"], value)
    raise DocumentError("This visual has no category field to color by value")


def _field_projection(model: ModelInfo, ref: FieldRef, measure_role: bool) -> dict[str, Any]:
    kind, table, name = model.resolve(ref)
    if kind == "measure":
        field_expr = expr.measure_ref(table, name)
        query_ref = f"{table}.{name}"
    elif measure_role and (ref.aggregation or model.is_numeric(table, name)):
        function = ref.aggregation or "Sum"
        codes = {"Sum": 0, "Average": 1, "Count": 2, "Min": 3, "Max": 4, "CountNonNull": 5}
        field_expr = {
            "Aggregation": {
                "Expression": expr.column_ref(table, name),
                "Function": codes[function],
            }
        }
        query_ref = f"{function}({table}.{name})"
    else:
        field_expr = expr.column_ref(table, name)
        query_ref = f"{table}.{name}"
    return {"field": field_expr, "queryRef": query_ref, "nativeQueryRef": name}


def _page_bounds(page: PageDoc) -> tuple[float, float]:
    return float(page.data.get("width", 1280)), float(page.data.get("height", 720))


def _z_values(page: PageDoc) -> list[float]:
    return [float(v.get("position", {}).get("z", 0)) for v in page.visuals.values()]


_DISPLAY_UNITS = {
    "auto": 0,
    "none": 1,
    "thousands": 1000,
    "millions": 1000000,
    "billions": 1000000000,
}


def _visual_op(document: PbirDocument, visual_id: str) -> tuple[PageDoc, dict[str, Any]]:
    page, visual = document.visual(visual_id)
    if "visual" not in visual:
        raise DocumentError("Visual groups hold other visuals; edit the visuals inside them")
    return page, visual
