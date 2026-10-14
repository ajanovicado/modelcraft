"""Read-only business help grounded in a report's preserved model and visuals.

These helpers never call a model, open an archive, run a query or apply an edit. A caller
may supply its existing number-explanation context or full-snapshot relationship governance.
Recommendations are reviewable proposals, not receipts. Apply them only through the normal
head/freshness checks and edit confirmation path. Partition and M bodies are never read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, fields, is_dataclass
from typing import Any, Literal, cast

from ai_powerbi_analyst.contracts import SourceManifest

from . import dax_describe, dax_syntax, model_health, number_explain
from .edit_context import EditContext, type_name
from .relationship_engine import RelationshipGovernanceResult
from .report_edit_model import CreateRelationship
from .report_import import ImportedReport, ImportedVisual


@dataclass(frozen=True)
class RoleField:
    """A native field well and the canonical model field that belongs in it."""

    role: Literal["Category", "Y", "Data"]
    kind: Literal["measure", "column"]
    table: str
    name: str
    aggregation: Literal["Sum", "Average", "Count"] | None = None

    def field(self) -> dict[str, str]:
        result = {"table": self.table, "name": self.name}
        if self.aggregation:
            result["aggregation"] = self.aggregation
        return result


@dataclass(frozen=True)
class VisualRecommendation:
    kind: Literal["recommend", "clarify"]
    text: str
    visual_type: Literal["lineChart", "clusteredBarChart", "cardVisual", "donutChart"] | None = None
    roles: tuple[RoleField, ...] = ()


@dataclass(frozen=True)
class ModelProposal:
    kind: Literal["relationship", "date_table", "star_schema", "health_fix"]
    text: str
    operations: tuple[dict[str, Any], ...] = ()
    blockers: tuple[str, ...] = ()
    # Only full-snapshot governance can set this; name matching is never key verification.
    verified: bool = False
    requires_desktop: bool = False


@dataclass(frozen=True)
class ModelAdvice:
    text: str
    proposals: tuple[ModelProposal, ...] = ()


@dataclass(frozen=True)
class _Field:
    kind: Literal["measure", "column"]
    table: str
    name: str
    dtype: str = ""
    expression: str = ""

    @property
    def label(self) -> str:
        return f"{self.table}[{self.name}]"


def _words(text: str) -> str:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", spaced.casefold()))


def _has(message: str, name: str) -> bool:
    named = _words(name)
    return bool(named) and f" {named} " in f" {_words(message)} "


def _schema(report: ImportedReport, context: EditContext | None = None) -> list[_Field]:
    result: dict[tuple[str, str, str], _Field] = {}
    for row in report.tables:
        table, name = str(row.get("TableName") or ""), str(row.get("ColumnName") or "")
        if table and name:
            item = _Field("column", table, name, str(row.get("PandasDataType") or ""))
            result[(item.kind, table.casefold(), name.casefold())] = item
    facts = report.model or {}
    for row in facts.get("columns", []):
        table, name = str(row.get("table") or ""), str(row.get("name") or "")
        if table and name:
            dtype = str(row.get("dateTimeType") or row.get("dataType") or "")
            item = _Field("column", table, name, dtype)
            result[(item.kind, table.casefold(), name.casefold())] = item
    if context is not None:
        for table, columns in context.tables.items():
            for name in columns:
                key = ("column", table.casefold(), name.casefold())
                if key not in result:
                    result[key] = _Field(
                        "column", table, name, context.column_types.get(f"{table}.{name}", "")
                    )
    for row in report.measures:
        table, name = str(row.get("TableName") or ""), str(row.get("Name") or "")
        if name:
            item = _Field("measure", table, name, expression=str(row.get("Expression") or ""))
            result[(item.kind, table.casefold(), name.casefold())] = item
    # Imported reports predating the current flattened fields can still carry full facts.
    for row in facts.get("measures", []):
        table, name = str(row.get("table") or ""), str(row.get("name") or "")
        key = ("measure", table.casefold(), name.casefold())
        if name and key not in result:
            result[key] = _Field(
                "measure", table, name, expression=str(row.get("expression") or "")
            )
    return list(result.values())


def _children(node: object) -> Iterator[object]:
    if is_dataclass(node):
        for member in fields(node):
            yield from _children(getattr(node, member.name))
        yield node
    elif isinstance(node, tuple):
        for child in node:
            yield from _children(child)


def _origins(metric: _Field, schema: list[_Field], seen: set[str] | None = None) -> set[str]:
    if metric.kind == "column":
        return {metric.table.casefold()}
    seen = set() if seen is None else seen
    key = metric.name.casefold()
    if key in seen:
        return set()
    seen.add(key)
    try:
        nodes = list(_children(dax_syntax.parse_expression(metric.expression)))
    except ValueError:
        return set()
    known = {f.table.casefold() for f in schema if f.kind == "column"}
    origins = {
        n.table.casefold()
        for n in nodes
        if isinstance(n, dax_syntax.ColRef)
        and n.table is not None
        and n.table.casefold() in known
        and any(
            f.kind == "column"
            and f.table.casefold() == n.table.casefold()
            and f.name.casefold() == n.column.casefold()
            for f in schema
        )
    }
    origins |= {
        n.name.casefold()
        for n in nodes
        if isinstance(n, dax_syntax.Name) and n.name.casefold() in known
    }
    for node in nodes:
        if not isinstance(node, dax_syntax.ColRef):
            continue
        matches = [
            f
            for f in schema
            if f.kind == "measure"
            and f.name.casefold() == node.column.casefold()
            and (node.table is None or f.table.casefold() == node.table.casefold())
        ]
        if len(matches) == 1:
            origins |= _origins(matches[0], schema, seen)
    return origins


def _relationships(report: ImportedReport) -> list[dict[str, Any]]:
    facts = report.model or {}
    relationships = facts.get("relationships")
    if isinstance(relationships, list):
        return cast(list[dict[str, Any]], relationships)
    return [
        {
            "fromTable": r.get("FromTableName", r.get("fromTable", "")),
            "fromColumn": r.get("FromColumnName", r.get("fromColumn", "")),
            "toTable": r.get("ToTableName", r.get("toTable", "")),
            "toColumn": r.get("ToColumnName", r.get("toColumn", "")),
            "active": (active is True or type(active) is int and active == 1),
            "cardinality": r.get("Cardinality", r.get("cardinality", "unknown")),
        }
        for r in report.relationships
        for active in [r.get("IsActive", r.get("active"))]
    ]


def _can_group(report: ImportedReport, origins: set[str], table: str) -> bool:
    """An active many-to-one path carries a dimension's filters to a metric's source.

    Never infer a join from shared column names or use an inactive path. A multi-fact
    measure needs the grouping table to reach every source, not just one component.
    """
    if not origins:
        return False
    edges: dict[str, set[str]] = {}
    for rel in _relationships(report):
        if rel.get("active") is not True:
            continue
        cardinality = str(rel.get("cardinality") or "").casefold()
        from_card = str(rel.get("fromCardinality") or "").casefold()
        to_card = str(rel.get("toCardinality") or "").casefold()
        if cardinality not in {"m:1", "1:m", "many_to_one", "1:1", "one_to_one"} and not (
            from_card == "many" and to_card == "one"
        ):
            continue
        left, right = (
            str(rel.get("fromTable") or "").casefold(),
            str(rel.get("toTable") or "").casefold(),
        )
        if cardinality == "1:m":
            left, right = right, left
        edges.setdefault(left, set()).add(right)
        if cardinality in {"1:1", "one_to_one"}:
            edges.setdefault(right, set()).add(left)
    for source in origins:
        reachable, pending = {source}, [source]
        while pending:
            current = pending.pop()
            for neighbour in edges.get(current, set()) - reachable:
                reachable.add(neighbour)
                pending.append(neighbour)
        if table.casefold() not in reachable:
            return False
    return True


def _meaning(metric: _Field, schema: list[_Field]) -> list[str]:
    expressions = {f.name: f.expression for f in schema if f.kind == "measure"}
    meaning = dax_describe.describe(metric.expression, set(expressions))
    parts = dax_describe.describe_parts(metric.expression, set(expressions)) if not meaning else []
    lines = [
        f"{metric.label}: {meaning}."
        if meaning
        else f"{metric.label}: Studio cannot give a reliable plain-language meaning for the "
        "whole formula; inspect its definition in DAX Lab."
    ]
    lines += [f"{name}: {text}." for name, text in parts]
    for name in dax_describe.referenced_measures(metric.expression, expressions)[:6]:
        detail = dax_describe.describe(expressions[name], set(expressions))
        lines.append(f"Uses [{name}]" + (f": {detail}." if detail else "."))
    return lines


_WALK = re.compile(
    r"\b(?:explain|describe|walk\s+(?:me\s+)?through|what\s+(?:does|is)|how\s+(?:does|is))\b",
    re.I,
)


def _walk_subject(message: str) -> tuple[str | None, str]:
    # A field identity can itself contain "Page", "Measure" or "Model".
    text = re.sub(r"(?:'[^']+'|[\w#]+)\s*\[[^\]]+\]", " ", message)
    match = re.search(
        r"\b(model|measures?|calculations?|formulas?|visual|chart|card|"
        r"table|slicer|page)\b",
        text,
        re.I,
    )
    return (match[1].casefold().removesuffix("s") if match else None, text)


def _model_walkthrough_request(message: str) -> bool:
    # Existing model_answers owns the richer factual relationship/key/parameter
    # walkthrough. A chart or slicer mentioned inside it is not the primary subject.
    return _WALK.search(message) is not None and _walk_subject(message)[0] == "model"


def _visual_lines(visual: ImportedVisual, schema: list[_Field]) -> list[str]:
    label = visual.caption or visual.title or visual.id
    hidden = " It is saved as hidden." if visual.hidden else ""
    lines = [f"{label} is a {type_name(visual.visual_type)}.{hidden}"]
    for binding in visual.bindings[:12]:
        table, name = str(binding.get("table") or ""), str(binding.get("name") or "")
        if not name:
            continue
        role = str(binding.get("role") or binding.get("kind") or "Field")
        lines.append(f"{role}: {table}[{name}].")
        matches = [f for f in schema if f.kind == "measure" and f.table == table and f.name == name]
        if len(matches) == 1:
            lines += _meaning(matches[0], schema)
    filters = visual.definition.get("filterConfig", {}).get("filters", [])
    if isinstance(filters, list) and filters:
        lines.append(
            f"It stores {len(filters)} visual filter definition(s); page and report "
            "filters can also affect its values."
        )
    if not visual.bindings:
        lines.append("It has no model field bindings; it does not define a calculated metric.")
    return lines


def walkthrough(
    message: str,
    report: ImportedReport,
    *,
    context: EditContext | None = None,
    page_id: str | None = None,
    visual_id: str | None = None,
    measure: str | None = None,
    explain_context: number_explain.AskContext | None = None,
) -> str | None:
    """Explain an existing page, visual or measure. None leaves other questions to chat.

    ``measure`` is a canonical name or ``Table[Name]``, never a client formula. A supplied
    AskContext uses the caller's existing engine/request state for number explanations;
    definition-only walkthroughs make no claim about current rendered numbers.
    """
    if _model_walkthrough_request(message):
        return None
    walk_ask = _WALK.search(message) is not None
    schema = _schema(report, context)
    page_id = page_id or (context.active_page if context else None)
    visual_id = visual_id or (context.selection if context else None)
    visuals = [(p, v) for p in report.pages for v in p.visuals]
    named = [
        (p, v) for p, v in visuals if any(_has(message, n) for n in (v.caption or "", v.title) if n)
    ]
    named_pages = [p for p in report.pages if _has(message, p.title)]
    # A selected attachment supplies deictic context; an explicit subject still wins.
    subject, subject_text = _walk_subject(message)
    page_ask = subject == "page" or subject is None and bool(named_pages)
    visual_ask = subject in {"visual", "chart", "card", "table", "slicer"}
    measures = [f for f in schema if f.kind == "measure"]
    explicit_measures = _named_fields(message, measures, fuzzy=False)
    qualified_ask = bool(re.search(r"(?:'[^']+'|[\w#]+)\s*\[[^\]]+\]", message))
    measure_ask = subject in {"measure", "calculation", "formula"} or (
        subject is None
        and not named
        and not named_pages
        and (bool(explicit_measures) or qualified_ask or measure is not None)
    )
    if walk_ask and measure_ask:
        if _unknown_qualified_field(message, schema):
            return (
                "Which measure do you want explained? A qualified field is not in this "
                "report; use its current table and exact measure name."
            )
        plural = re.search(r"\b(?:measures|calculations|formulas)\b", subject_text, re.I)
        different_names = {field.name.casefold() for field in explicit_measures}
        qualified_count = sum(
            bool(
                re.search(
                    rf"(?<!\w)'?{re.escape(field.table)}'?\s*\[\s*{re.escape(field.name)}\s*\]",
                    message,
                    re.I,
                )
            )
            for field in explicit_measures
        )
        if plural or len(different_names) > 1 or qualified_count > 1:
            # This closed helper explains one scalar. Preserve the whole multi-measure
            # question for the existing answer lane, including fenced author metadata.
            # A single bare name matching several tables still clarifies below.
            return None
    number_words = _words(subject_text)
    for identity in [
        *(f.name for f in explicit_measures),
        *(p.title for p in named_pages),
        *(v.caption or v.title for _, v in named),
    ]:
        if _words(identity):
            number_words = re.sub(r"\b" + re.escape(_words(identity)) + r"\b", " ", number_words)
    definition_ask = (
        walk_ask
        and (
            page_ask
            or subject in {"measure", "calculation", "formula"}
            or bool(explicit_measures)
            or qualified_ask
            or bool(named)
        )
        and not re.search(
            r"\b(?:why|number|value|figure|result|blank|empty|missing|zero|negative|"
            r"colou?r|red|green|blue|orange|yellow|purple|pink|grey|gray|black|white|teal)\b",
            number_words,
            re.I,
        )
    )
    if (
        explain_context is not None
        and number_explain.is_explain_question(message)
        and not definition_ask
    ):
        if (
            explain_context.report.id != report.id
            or explain_context.report.base_hash != report.base_hash
            or explain_context.report.revision_hash != report.revision_hash
        ):
            return "The number context belongs to a different report or revision; select it again."
        answer = number_explain.answer(message, explain_context)
        if answer is not None:
            return answer
    if not walk_ask:
        return None
    if measure_ask:
        matches = explicit_measures
        deictic = bool(
            re.search(
                r"\b(?:this|that|selected|current)\s+(?:measure|calculation|formula)\b"
                r"|\b(?:explain|describe|through)\s+(?:the\s+)?(?:measure|calculation|formula)\s*[?.!]*$",
                message,
                re.I,
            )
        )
        if not matches and measure is not None and (deictic or subject is None):
            matches = [
                f for f in measures if measure.casefold() in {f.name.casefold(), f.label.casefold()}
            ]
        if len(matches) != 1:
            return "Which measure do you want explained? Use its table and exact measure name."
        return "\n".join(
            [
                *_meaning(matches[0], schema),
                "Its value is evaluated under the filters of each visual that uses it.",
            ]
        )
    if not page_ask and (visual_ask or named or visual_id):
        if (
            not named
            and not visual_id
            and not re.search(
                r"\b(?:this|that|selected|current)\s+(?:visual|chart|card|table|slicer)\b"
                r"|\bthe\s+(?:visual|chart|card|table|slicer)\b",
                message,
                re.I,
            )
        ):
            return None  # An unanchored how-to question still belongs to ordinary chat.
        matching_visuals = named or [(p, v) for p, v in visuals if v.id == visual_id]
        if len(matching_visuals) != 1:
            return "Which visual do you want explained? Select it on the canvas."
        page, visual = matching_visuals[0]
        if page_id and visual_id and visual.id == visual_id and page.id != page_id:
            return "That visual does not belong to the attached page; select it again."
        return "\n".join(
            [
                *_visual_lines(visual, schema),
                "This describes the saved definition. Select a number to explain its "
                "value with the relevant filters.",
            ]
        )
    if page_ask:
        pages = named_pages or [p for p in report.pages if p.id == page_id]
        if len(pages) != 1:
            return "Which page do you want walked through? Select it or use its exact name."
        page = pages[0]
        lines = [f"Page {page.title} contains {len(page.visuals)} saved visual(s)."]
        for visual in sorted(page.visuals, key=lambda v: (v.y, v.x, v.z, v.id))[:12]:
            lines += _visual_lines(visual, schema)
        if len(page.visuals) > 12:
            lines.append(f"{len(page.visuals) - 12} more visuals are on this page.")
        lines.append(
            "This is a walkthrough of the saved definitions, not a calculation of "
            "the current canvas selections."
        )
        return "\n".join(lines)
    return None


_CHART_ASK = re.compile(
    r"\b(?:visuali[sz]e|chart|graph|plot|visual|card|trend|best\s+(?:way|display)|show|display)\b",
    re.I,
)
_GENERIC_METRIC_WORDS = {"total", "sum", "amount", "of", "the"}


def _named_fields(
    message: str,
    candidates: list[_Field],
    *,
    fuzzy: bool = True,
) -> list[_Field]:
    qualified = [
        f
        for f in candidates
        if re.search(
            rf"(?<!\w)'?{re.escape(f.table)}'?\s*\[\s*{re.escape(f.name)}\s*\]", message, re.I
        )
    ]
    # Keep separately named bare fields, but never reinterpret a qualified spelling's
    # table/name as an unqualified field from another table.
    bare_message = re.sub(r"(?:'[^']+'|[\w#]+)\s*\[[^\]]+\]", " ", message)
    normalized = _words(bare_message)
    spans = [
        (f, match.span())
        for f in candidates
        for match in re.finditer(rf"(?<!\w){re.escape(_words(f.name))}(?!\w)", normalized)
    ]
    exact = [
        f
        for f in candidates
        if any(
            own == f
            and not any(
                start <= a and b <= end and (a, b) != (start, end)
                for other, (start, end) in spans
                if other != own
            )
            for own, (a, b) in spans
        )
    ]
    if not exact and fuzzy and not qualified:
        asked = set(normalized.split())
        exact = [
            f
            for f in candidates
            if (core := set(_words(f.name).split()) - _GENERIC_METRIC_WORDS) and core <= asked
        ]
    return list(dict.fromkeys([*qualified, *exact]))


def _date(field: _Field) -> bool:
    return field.dtype.casefold() in {"9", "date", "datetime", "datetime64[ns]", "datetime64[us]"}


def _unknown_qualified_field(message: str, schema: list[_Field]) -> bool:
    known_spans = [
        match.span()
        for field in schema
        for match in re.finditer(
            rf"(?<!\w)'?{re.escape(field.table)}'?\s*\[\s*{re.escape(field.name)}\s*\]",
            message,
            re.I,
        )
    ]
    for match in re.finditer(r"(?:'[^']+'|[\w#]+)\s*\[[^\]]+\]", message):
        if not any(start <= match.start() and match.end() <= end for start, end in known_spans):
            return True
    return False


def recommend_visual(
    message: str, report: ImportedReport, *, context: EditContext | None = None
) -> VisualRecommendation | None:
    """Recommend a closed native chart and canonical field wells; ambiguity emits no fields.

    A caller can preview ``roles`` and use ``role.field()`` with its existing add_visual
    tool after explicit user authorization. This function neither applies nor offers an
    operation that silently chooses an aggregation, date relationship or missing field.
    """
    if _model_walkthrough_request(message) or not _CHART_ASK.search(message):
        return None
    words = _words(message)
    trend = bool(
        re.search(r"\b(?:trend|over time|monthly|daily|weekly|yearly|time series)\b", words)
    )
    share = bool(
        re.search(r"\b(?:share|contribution|portion|mix|part of|parts of|part to whole)\b", words)
    )
    compare = bool(
        re.search(r"\b(?:compare|comparison|rank|ranking|most|least|across|breakdown|by)\b", words)
    )
    single = bool(re.search(r"\b(?:single|headline|one number|overall|kpi|card)\b", words))
    families = sum((trend, share, single))
    if families > 1:
        return VisualRecommendation(
            "clarify",
            "Do you want a time trend, a share breakdown, "
            "or one headline number? Choose one for this visual.",
        )
    if not (trend or share or compare or single):
        return None
    schema = _schema(report, context)
    if _unknown_qualified_field(message, schema):
        return VisualRecommendation(
            "clarify",
            "A qualified field in this request is not in "
            "this report. Use the current table and exact field name.",
        )
    measures = [f for f in schema if f.kind == "measure"]
    metric = _named_fields(message, measures)
    aggregation: Literal["Sum", "Average", "Count"] | None = None
    if re.search(r"\b(?:total|sum|average)\b", words):
        numeric = [
            f
            for f in schema
            if f.kind == "column"
            and (f.dtype in {"6", "8", "10"} or re.match(r"(?:int|float|decimal)", f.dtype))
        ]
        explicit_columns = _named_fields(message, numeric)
        # Sales[Amount] names a numeric column, not the "sales" component of Total Sales.
        # Only explicit canonical columns override an existing measure's name.
        qualified_columns = [
            f
            for f in explicit_columns
            if re.search(
                rf"(?<!\w)'?{re.escape(f.table)}'?\s*\[\s*{re.escape(f.name)}\s*\]", message, re.I
            )
        ]
        if qualified_columns or not metric:
            metric = qualified_columns or explicit_columns
            aggregation = "Average" if re.search(r"\baverage\b", words) else "Sum"
    if (
        not metric
        and context
        and context.selection
        and re.search(
            r"\b(?:this|that|selected)\s+(?:metric|measure|value|chart|visual)\b", message, re.I
        )
    ):
        selected = next(
            (v for p in report.pages for v in p.visuals if v.id == context.selection), None
        )
        if selected:
            metric = [
                f
                for f in measures
                if any(
                    b.get("kind") == "measure"
                    and b.get("table") == f.table
                    and b.get("name") == f.name
                    for b in selected.bindings
                )
            ]
    if len(metric) != 1:
        names = ", ".join(f.label for f in metric[:4])
        return VisualRecommendation(
            "clarify",
            "Which existing measure should the visual use?"
            + (f" These match: {names}." if names else " Name it exactly."),
        )
    selected_metric = metric[0]
    value = RoleField(
        "Data" if single else "Y",
        selected_metric.kind,
        selected_metric.table,
        selected_metric.name,
        aggregation,
    )
    if single:
        return VisualRecommendation(
            "recommend",
            f"Use a card for the single headline value "
            f"{selected_metric.label}. This is a recommendation; "
            "the report has not changed.",
            "cardVisual",
            (value,),
        )
    origins = _origins(selected_metric, schema)
    columns = [f for f in schema if f.kind == "column"]
    named = _named_fields(message, columns)
    if trend:
        extra = [f for f in named if not _date(f) and f != selected_metric]
        if extra:
            return VisualRecommendation(
                "clarify",
                "Do you want one total time trend or "
                "separate trends by a category? This recommendation "
                "needs one date axis and one metric; choose the grouping.",
            )
        named = [f for f in named if _date(f)]
        possible = [f for f in columns if _date(f) and _can_group(report, origins, f.table)]
        if not named:
            marked = {
                str(t.get("name")).casefold()
                for t in (report.model or {}).get("tables", [])
                if t.get("isDateTable") is True
            }
            preferred = [f for f in possible if f.table.casefold() in marked]
            named = preferred or possible
    else:
        named = [f for f in named if not _date(f) and f != selected_metric]
    if len(named) != 1:
        need = "date column" if trend else "category column"
        return VisualRecommendation(
            "clarify",
            f"Which {need} should group {selected_metric.label}? "
            "Use its table and exact column name.",
        )
    group = named[0]
    if not _can_group(report, origins, group.table):
        return VisualRecommendation(
            "clarify",
            f"Studio cannot verify an active filter path "
            f"from {group.label} to the source of {selected_metric.label}. "
            "Review the relationship before grouping them.",
        )
    if share:
        # A ratio or average cannot be added as slices of a total merely because its name fits.
        try:
            node = dax_syntax.parse_expression(selected_metric.expression)
        except ValueError:
            node = None
        additive = (
            aggregation == "Sum"
            or isinstance(node, dax_syntax.Call)
            and node.name in {"SUM", "SUMX", "COUNT", "COUNTROWS"}
        )
        if not additive:
            return VisualRecommendation(
                "clarify",
                "A part-to-whole chart needs an additive "
                "amount or count. Choose a total measure rather than "
                "an average, ratio, rank or distinct count.",
            )
    chart: Literal["lineChart", "donutChart", "clusteredBarChart"] = (
        "lineChart" if trend else "donutChart" if share else "clusteredBarChart"
    )
    purpose = (
        "time trend" if trend else "part-to-whole breakdown" if share else "category comparison"
    )
    roles = (RoleField("Category", "column", group.table, group.name), value)
    return VisualRecommendation(
        "recommend",
        f"Use a {type_name(chart)} for this {purpose}: "
        f"{group.label} groups {selected_metric.label}. "
        "This is a recommendation; the report has not changed.",
        chart,
        roles,
    )


_MODEL_ASK = re.compile(
    r"\b(?:relationships?|join(?:ing)?|connect(?:ing)?|link(?:ing)?|star\s+schema|"
    r"date\s+table|calendar\s+table|"
    r"model\s+(?:design|build|advice)|build\s+(?:a\s+|the\s+)?model)\b",
    re.I,
)


def model_advice(
    message: str,
    report: ImportedReport,
    *,
    manifest: SourceManifest | None = None,
    governance: RelationshipGovernanceResult | None = None,
    health: model_health.Health | None = None,
) -> ModelAdvice | None:
    """Explain a business model plan, with optional full-snapshot governed proposals.

    Supply the caller's current matching manifest and governance result together. Candidate
    operations are proposals only and still require current-head/data revalidation before
    application. A date table is guidance until its creation/marking path is verified locally.
    """
    if _model_walkthrough_request(message) or not _MODEL_ASK.search(message):
        return None
    health = health or model_health.check_model(report.model, usage=None)
    lines: list[str] = []
    proposals: list[ModelProposal] = []
    date_ask = bool(re.search(r"\b(?:date|calendar)\s+table\b", message, re.I))
    star_ask = bool(
        re.search(
            r"\b(?:star\s+schema|model\s+(?:design|build|advice)|build\s+"
            r"(?:a\s+|the\s+)?model)\b",
            message,
            re.I,
        )
    )
    if star_ask:
        text = (
            "Keep transactions in fact tables and descriptive attributes in dimensions. "
            "Relate each fact's key to a verified unique dimension key using a single "
            "filter direction. Shared dimensions should filter separate facts; do not "
            "join fact tables directly or invent a bridge from matching names."
        )
        lines.append(text)
        proposals.append(ModelProposal("star_schema", text))
    if date_ask or star_ask:
        marked = [
            str(t.get("name"))
            for t in (report.model or {}).get("tables", [])
            if t.get("isDateTable") is True
        ]
        text = "Marked date tables in this model: " + ", ".join(marked) + ". " if marked else ""
        text += (
            "Use one date row per day, with no blanks or duplicates and continuous dates "
            "covering the fact data. Connect its date key to the intended fact date; "
            "keep alternate date relationships inactive and use USERELATIONSHIP for "
            "those measures. Date-table creation and marking need a verified local "
            "Desktop path; this advice does not create a table."
        )
        lines.append(text)
        proposals.append(ModelProposal("date_table", text, requires_desktop=True))
    schema = _schema(report)
    existing = {
        frozenset(
            (
                (str(r.get("fromTable")).casefold(), str(r.get("fromColumn")).casefold()),
                (str(r.get("toTable")).casefold(), str(r.get("toColumn")).casefold()),
            )
        )
        for r in _relationships(report)
    }
    if governance is not None and manifest is not None:
        tables = {t.id: t for t in manifest.tables}
        for candidate in governance.relationships[:12]:
            if candidate.review_status == "rejected":
                continue
            left, right = tables.get(candidate.from_table_id), tables.get(candidate.to_table_id)
            if left is None or right is None:
                continue
            from_column = next(
                (c.name for c in left.columns if c.id == candidate.from_column_id), None
            )
            to_column = next(
                (c.name for c in right.columns if c.id == candidate.to_column_id), None
            )
            if from_column is None or to_column is None:
                continue
            endpoints = ((left.name, from_column), (right.name, to_column))
            signature = frozenset((t.casefold(), c.casefold()) for t, c in endpoints)
            if signature in existing:
                continue
            blockers = list(candidate.blockers)
            if any(
                not any(
                    f.kind == "column"
                    and f.table.casefold() == table.casefold()
                    and f.name.casefold() == column.casefold()
                    for f in schema
                )
                for table, column in endpoints
            ):
                blockers.append("Candidate fields are not present in this report's current model.")
            if candidate.cardinality == "many_to_many" and not blockers:
                blockers.append(
                    "A many-to-many join needs a separately reviewed bridge or dimension."
                )
            op = CreateRelationship(
                from_table=left.name,
                from_column=from_column,
                to_table=right.name,
                to_column=to_column,
                cardinality=candidate.cardinality,
                cross_filter=candidate.cross_filter_direction,
                active=candidate.is_active,
            ).model_dump(exclude_defaults=True)
            op["op"] = "create_relationship"
            text = (
                f"{left.name}[{from_column}] to {right.name}[{to_column}]: "
                f"{candidate.cardinality.replace('_', '-')} with "
                f"{candidate.cross_filter_direction} filtering, "
                f"{'active' if candidate.is_active else 'inactive'}."
            )
            text += (
                " Blocked: " + "; ".join(blockers) + "."
                if blockers
                else " Full-snapshot governance checked this candidate; review it before applying."
            )
            lines.append(text)
            proposals.append(
                ModelProposal(
                    "relationship",
                    text,
                    () if blockers else (op,),
                    tuple(blockers),
                    not blockers,
                    True,
                )
            )
        lines.extend(governance.recommendations[:6])
    else:
        lines.append(
            "Relationship names alone do not prove compatible types, key uniqueness "
            "or matching rows. Full-snapshot governance is needed before a new join "
            "can be proposed for application."
        )
    for finding in health.findings:
        if finding.rule not in {
            "date_table_not_marked",
            "many_to_many_relationship",
            "bidirectional_relationship",
            "relationship_key_duplicates",
            "relationship_key_blanks",
        }:
            continue
        lines.append(finding.message + " " + finding.fix_summary)
        fix_blockers = (
            ("This fix needs a verified local Desktop capability probe.",)
            if finding.fix_outside_supported
            else ()
        )
        proposals.append(
            ModelProposal(
                "health_fix",
                finding.fix_summary,
                () if fix_blockers else tuple(finding.fix or ()),
                fix_blockers,
                False,
                finding.fix_needs_desktop,
            )
        )
    if not health.known:
        lines.append(
            "Studio could not read the full model facts, so model-health checks "
            "cannot establish this report's current design."
        )
    lines.append("These are recommendations and proposals. The report has not changed.")
    return ModelAdvice("\n".join(lines), tuple(proposals))
