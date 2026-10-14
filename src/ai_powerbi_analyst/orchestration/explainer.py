"""Read-only explanations grounded in the active PBIR and TMDL artifacts."""

from __future__ import annotations

import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ai_powerbi_analyst.contracts import MetricDefinition, SemanticCatalog

from .conversation import ConversationState

_MAX_METADATA_FILE_BYTES = 2 * 1024 * 1024
_MAX_TMDL_FILES = 512
_MAX_REPORT_PAGES = 256
_MAX_VISUALS_PER_PAGE = 512

_POWERBI_REFERENCE = re.compile(
    r"^\s*(?:'(?P<quoted_table>(?:[^']|'')+)'|(?P<table>[^'\[\]]+))"
    r"\[(?P<property>[^\[\]]+)\]\s*$"
)
_TMDL_IDENTIFIER = r"(?:'(?:[^']|'')+'|[^\s=]+(?: [^\s=]+)*)"
_TABLE_DECLARATION = re.compile(rf"^(?P<indent>[ \t]*)table\s+(?P<name>{_TMDL_IDENTIFIER})\s*$")
_MEASURE_DECLARATION = re.compile(
    rf"^(?P<indent>[ \t]*)measure\s+(?P<name>{_TMDL_IDENTIFIER})\s*=\s*(?P<rhs>.*)$"
)
_TMDL_MEASURE_PROPERTY = re.compile(
    r"^(?:formatString|description|displayFolder|dataCategory|summarizeBy|"
    r"formatStringDefinition|annotation|extendedProperty)(?::|\s|$)",
    re.IGNORECASE,
)
_SAFE_VISUAL_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class GroundedExplanationError(ValueError):
    """The requested explanation cannot be proven from active local artifacts."""


@dataclass(frozen=True, slots=True)
class _MeasureDefinition:
    table: str
    name: str
    dax: str
    source: Path


@dataclass(frozen=True, slots=True)
class _FieldBinding:
    kind: str
    table: str
    property_name: str


@dataclass(frozen=True, slots=True)
class _VisualUsage:
    page_title: str
    visual_title: str | None
    visual_type: str
    visual_id: str
    bindings: tuple[_FieldBinding, ...]


class GroundedPowerBIExplainer:
    """Explain one governed metric from exact active PBIR/TMDL metadata.

    The parser treats both artifact formats as untrusted text. It performs no
    metadata evaluation, DAX execution, arithmetic, or model generation.
    """

    def __init__(
        self,
        catalog: SemanticCatalog,
        *,
        report_path: str | Path,
        semantic_model_path: str | Path,
    ) -> None:
        self.catalog = catalog
        self.report_path = Path(report_path)
        self.semantic_model_path = Path(semantic_model_path)

    def explain(self, message: str, _state: ConversationState) -> str:
        """Implement the orchestration Explainer protocol from active artifacts."""

        page_titles = self._read_page_titles()
        normalized = _normalize_words(message)
        page = self._page_in_message(normalized, page_titles)
        if "scenario" in normalized and "forecast" in normalized:
            return self._explain_scenario_vs_forecast()
        if page is not None and "uncertainty" in normalized:
            return self._explain_uncertainty(page[0], page[1])
        if page is not None:
            return self._explain_page(page[0], page[1])

        metric_ids = self._metric_ids_in_message(message, page_titles)
        if len(metric_ids) != 1:
            raise GroundedExplanationError(
                "Name exactly one approved metric to receive a grounded formula explanation."
            )
        return self.explain_metric(next(iter(metric_ids)))

    def explain_metric(self, name_or_id: str) -> str:
        """Explain one approved metric and its current active-report usage."""

        try:
            metric = self.catalog.resolve_metric(name_or_id)
        except KeyError as error:
            raise GroundedExplanationError(str(error).strip("'")) from error

        table_name, measure_name = _parse_powerbi_reference(metric.powerbi_ref)
        dax = self._stored_dax(
            metric.powerbi_ref,
            definitions=self._read_measure_definitions(),
        )

        usages = self._read_visual_usages(table_name, measure_name)
        return self._render(metric, dax, usages)

    def _stored_dax(
        self,
        powerbi_ref: str,
        *,
        definitions: list[_MeasureDefinition],
    ) -> str:
        table_name, measure_name = _parse_powerbi_reference(powerbi_ref)
        matches = [
            definition
            for definition in definitions
            if definition.table.casefold() == table_name.casefold()
            and definition.name.casefold() == measure_name.casefold()
        ]
        if len(matches) != 1:
            raise GroundedExplanationError(
                f"Expected exactly one stored definition for {powerbi_ref}; found {len(matches)}."
            )
        dax = matches[0].dax
        if not dax:
            raise GroundedExplanationError(
                f"The stored definition for {powerbi_ref} has no DAX expression."
            )
        return dax

    def _read_measure_definitions(self) -> list[_MeasureDefinition]:
        definition_root = self.semantic_model_path / "definition"
        paths = _bounded_files(
            definition_root,
            "*.tmdl",
            maximum=_MAX_TMDL_FILES,
            label="TMDL",
            recursive=True,
        )
        definitions: list[_MeasureDefinition] = []
        for path in paths:
            text = _read_bounded_text(path, "TMDL")
            definitions.extend(_parse_tmdl_measures(text, path))
        return definitions

    def _read_page_titles(self) -> dict[Path, str]:
        pages_root = self.report_path / "definition" / "pages"
        page_files = _bounded_files(
            pages_root,
            "page.json",
            maximum=_MAX_REPORT_PAGES,
            label="PBIR page",
            recursive=False,
        )
        titles: dict[Path, str] = {}
        for page_file in page_files:
            value = _read_json_object(page_file, "PBIR page")
            title = value.get("displayName")
            if not isinstance(title, str):
                raise GroundedExplanationError(
                    f"PBIR page metadata is missing a displayName: {page_file}"
                )
            titles[page_file.parent] = _safe_label(title, "PBIR page title")
        return titles

    def _read_visual_usages(self, table_name: str, measure_name: str) -> list[_VisualUsage]:
        usages: list[_VisualUsage] = []
        for page_path, page_title in self._read_page_titles().items():
            for usage in self._read_page_visuals(page_path, page_title):
                if not any(
                    binding.kind == "measure"
                    and binding.table.casefold() == table_name.casefold()
                    and binding.property_name.casefold() == measure_name.casefold()
                    for binding in usage.bindings
                ):
                    continue
                usages.append(usage)
        return sorted(
            usages,
            key=lambda usage: (
                usage.page_title.casefold(),
                (usage.visual_title or "").casefold(),
                usage.visual_type.casefold(),
                usage.visual_id,
            ),
        )

    def _read_page_visuals(self, page_path: Path, page_title: str) -> list[_VisualUsage]:
        visuals_root = page_path / "visuals"
        visual_files = _bounded_files(
            visuals_root,
            "visual.json",
            maximum=_MAX_VISUALS_PER_PAGE,
            label="PBIR visual",
            recursive=False,
            missing_ok=True,
        )
        usages: list[_VisualUsage] = []
        for visual_file in visual_files:
            value = _read_json_object(visual_file, "PBIR visual")
            visual = value.get("visual")
            if not isinstance(visual, dict):
                raise GroundedExplanationError(
                    f"A PBIR visual is missing its visual object: {visual_file}"
                )
            visual_type = visual.get("visualType")
            if not isinstance(visual_type, str) or not _SAFE_VISUAL_TYPE.fullmatch(visual_type):
                raise GroundedExplanationError(
                    f"A PBIR visual has an invalid visualType: {visual_file}"
                )
            raw_id = value.get("name", visual_file.parent.name)
            if not isinstance(raw_id, str):
                raise GroundedExplanationError(f"A PBIR visual has an invalid name: {visual_file}")
            usages.append(
                _VisualUsage(
                    page_title=page_title,
                    visual_title=_visual_title(visual, visual_file),
                    visual_type=visual_type,
                    visual_id=_safe_label(raw_id, "PBIR visual name"),
                    bindings=tuple(
                        sorted(
                            _structured_field_bindings(value, visual_file),
                            key=lambda binding: (
                                binding.kind,
                                binding.table.casefold(),
                                binding.property_name.casefold(),
                            ),
                        )
                    ),
                )
            )
        return sorted(
            usages,
            key=lambda usage: (
                (usage.visual_title or "").casefold(),
                usage.visual_type.casefold(),
                usage.visual_id,
            ),
        )

    def _page_in_message(
        self,
        normalized_message: str,
        page_titles: dict[Path, str],
    ) -> tuple[Path, str] | None:
        matches: list[tuple[Path, str]] = []
        for path, title in page_titles.items():
            normalized_title = _normalize_words(title)
            aliases = {normalized_title}
            if normalized_title.startswith("outlook"):
                aliases.add("outlook")
            if any(
                re.search(_phrase_pattern(alias), normalized_message, flags=re.IGNORECASE)
                for alias in aliases
            ):
                matches.append((path, title))
        if len(matches) > 1:
            raise GroundedExplanationError(
                "Name exactly one active report page for a grounded page explanation."
            )
        return matches[0] if matches else None

    def _explain_page(self, page_path: Path, page_title: str) -> str:
        normalized_title = _normalize_words(page_title)
        purposes = {
            "executive pulse": (
                "This active PBIR page is the approved executive overview for governed KPIs, "
                "monthly trend, regional performance, and prepared slicers."
            ),
            "margin drivers": (
                "This active PBIR page uses the governed contribution bridge, driver detail, "
                "and the reserved conversational-evidence slot."
            ),
            "outlook & decisions": (
                "This active PBIR page keeps the frozen approved forecast, uncertainty series, "
                "and mechanical price scenario visibly separate."
            ),
        }
        purpose = purposes.get(
            normalized_title,
            "This purpose is limited to the exact visuals and approved bindings stored in PBIR.",
        )
        visuals = self._read_page_visuals(page_path, page_title)
        approved = self._approved_bindings()
        lines = [
            f"Page: {_markdown_text(page_title)}",
            f"Grounded purpose: {purpose}",
            "Active PBIR visuals and approved bindings:",
        ]
        if not visuals:
            lines.append("- The active page currently contains no visual definitions.")
        for visual in visuals:
            title = visual.visual_title or f"untitled {visual.visual_id}"
            rendered_bindings: list[str] = []
            for binding in visual.bindings:
                key = (
                    binding.table.casefold(),
                    binding.property_name.casefold(),
                    binding.kind,
                )
                binding_text = approved.get(key)
                if binding_text is None:
                    raise GroundedExplanationError(
                        "An active PBIR visual contains a binding outside the approved catalog."
                    )
                rendered_bindings.append(binding_text)
            suffix = ", ".join(rendered_bindings) if rendered_bindings else "no data binding"
            lines.append(
                f"- visual {json.dumps(title, ensure_ascii=False)} ({visual.visual_type}): {suffix}"
            )
        return "\n".join(lines)

    def _approved_bindings(self) -> dict[tuple[str, str, str], str]:
        approved: dict[tuple[str, str, str], str] = {}
        definitions: list[tuple[str, str]] = [
            *((metric.powerbi_ref, "measure") for metric in self.catalog.metrics),
            *((dimension.powerbi_ref, "column") for dimension in self.catalog.dimensions),
            *((item.powerbi_ref, item.kind) for item in self.catalog.report_objects),
        ]
        for reference, kind in definitions:
            table, property_name = _parse_powerbi_reference(reference)
            key = (table.casefold(), property_name.casefold(), kind)
            rendered = f"{reference} ({kind})"
            if key in approved and approved[key] != rendered:
                raise GroundedExplanationError(
                    f"Approved catalog contains an ambiguous Power BI binding: {reference}"
                )
            approved[key] = rendered
        return approved

    def _explain_scenario_vs_forecast(self) -> str:
        definitions = self._read_measure_definitions()
        scenario = self.catalog.resolve_report_object("scenario_assumption")
        forecast = self.catalog.resolve_report_object("forecast_provenance")
        scenario_dax = self._stored_dax(scenario.powerbi_ref, definitions=definitions)
        forecast_dax = self._stored_dax(forecast.powerbi_ref, definitions=definitions)
        return "\n".join(
            [
                "The approved scenario is a precomputed mechanical sensitivity, not a "
                "forecast or causal prediction. The separate forecast object records the "
                "frozen approved forecasting provenance.",
                f"{scenario.name} exact stored DAX:",
                "",
                _indent_code(scenario_dax),
                "",
                f"{forecast.name} exact stored DAX:",
                "",
                _indent_code(forecast_dax),
            ]
        )

    def _explain_uncertainty(self, page_path: Path, page_title: str) -> str:
        if not _normalize_words(page_title).startswith("outlook"):
            raise GroundedExplanationError(
                "Forecast uncertainty is supported only on the active Outlook page."
            )
        object_ids = (
            "forecast_net_sales_lower_80",
            "forecast_net_sales_upper_80",
            "forecast_net_sales_lower_95",
            "forecast_net_sales_upper_95",
        )
        objects = [self.catalog.resolve_report_object(object_id) for object_id in object_ids]
        definitions = self._read_measure_definitions()
        visuals = self._read_page_visuals(page_path, page_title)
        target_keys = {(*_casefold_reference(item.powerbi_ref), "measure") for item in objects}
        line_visuals = [
            visual
            for visual in visuals
            if visual.visual_type == "lineChart"
            and target_keys.issubset(
                {
                    (
                        binding.table.casefold(),
                        binding.property_name.casefold(),
                        binding.kind,
                    )
                    for binding in visual.bindings
                }
            )
        ]
        if line_visuals:
            rendering = (
                "The active PBIR lineChart binds all four stored lower/upper series. This is "
                "a line-series rendering, not a native shaded-band claim."
            )
        else:
            rendering = (
                "The active Outlook page does not currently bind all four stored lower/upper "
                "series to one lineChart."
            )
        lines = [
            f"Page: {_markdown_text(page_title)}",
            "The 80% and 95% uncertainty values are separate frozen forecast measures; they "
            "are not scenario outputs.",
            rendering,
        ]
        for item in objects:
            dax = self._stored_dax(item.powerbi_ref, definitions=definitions)
            lines.extend([f"{item.name} exact stored DAX:", "", _indent_code(dax), ""])
        return "\n".join(lines).rstrip()

    def _metric_ids_in_message(
        self,
        message: str,
        page_titles: dict[Path, str],
    ) -> set[str]:
        normalized = _normalize_words(message)
        for title in sorted(page_titles.values(), key=len, reverse=True):
            normalized_title = _normalize_words(title)
            normalized = re.sub(
                _phrase_pattern(normalized_title),
                " ",
                normalized,
                flags=re.IGNORECASE,
            )

        matches: list[tuple[int, int, str]] = []
        for metric in self.catalog.metrics:
            for candidate in (metric.id, metric.name, *metric.aliases):
                label = _normalize_words(candidate)
                for match in re.finditer(
                    _phrase_pattern(label),
                    normalized,
                    flags=re.IGNORECASE,
                ):
                    matches.append((match.start(), match.end(), metric.id))

        accepted: list[tuple[int, int, str]] = []
        for start, end, metric_id in sorted(
            matches,
            key=lambda item: (-(item[1] - item[0]), item[0], item[2]),
        ):
            overlaps = any(
                start < accepted_end and end > accepted_start
                for accepted_start, accepted_end, _ in accepted
            )
            if overlaps:
                continue
            accepted.append((start, end, metric_id))
        return {metric_id for _, _, metric_id in accepted}

    def _render(
        self,
        metric: MetricDefinition,
        dax: str,
        usages: list[_VisualUsage],
    ) -> str:
        dependency_names = [
            self.catalog.resolve_metric(dependency).name for dependency in metric.dependencies
        ]
        explanation = metric.description
        if dependency_names:
            explanation += " Governed dependencies: " + ", ".join(dependency_names) + "."
        else:
            explanation += " It has no governed metric dependencies."

        lines = [
            f"Metric: {metric.name} ({metric.id})",
            f"Power BI binding: {metric.powerbi_ref}",
            "Exact stored DAX:",
            "",
            _indent_code(dax),
            "",
            f"Plain-language explanation: {explanation}",
            "Supported dimensions/filters: "
            + ", ".join(
                self.catalog.resolve_dimension(identifier).name
                for identifier in metric.allowed_dimensions
            )
            + ".",
            "Active report usage:",
        ]
        if usages:
            for usage in usages:
                visual_label = usage.visual_title or f"untitled {usage.visual_id}"
                lines.append(
                    f"- Page {json.dumps(usage.page_title, ensure_ascii=False)} — visual "
                    f"{json.dumps(visual_label, ensure_ascii=False)} ({usage.visual_type})"
                )
        else:
            lines.append("- This metric is not currently used by any active visual.")
        return "\n".join(lines)


def _parse_powerbi_reference(reference: str) -> tuple[str, str]:
    match = _POWERBI_REFERENCE.fullmatch(reference)
    if match is None:
        raise GroundedExplanationError(
            f"Approved metric has an invalid Power BI binding: {reference}"
        )
    raw_table = match.group("quoted_table") or match.group("table")
    table = _unquote_tmdl_identifier(raw_table, already_unquoted=match.group("table") is not None)
    property_name = match.group("property").strip()
    if not table or not property_name:
        raise GroundedExplanationError(
            f"Approved metric has an invalid Power BI binding: {reference}"
        )
    return table, property_name


def _parse_tmdl_measures(text: str, source: Path) -> list[_MeasureDefinition]:
    lines = text.splitlines()
    definitions: list[_MeasureDefinition] = []
    current_table: str | None = None
    index = 0
    while index < len(lines):
        line = lines[index]
        table_match = _TABLE_DECLARATION.fullmatch(line)
        if table_match is not None:
            current_table = _unquote_tmdl_identifier(table_match.group("name"))
            index += 1
            continue

        measure_match = _MEASURE_DECLARATION.fullmatch(line)
        if measure_match is None:
            index += 1
            continue
        if current_table is None:
            raise GroundedExplanationError(
                f"TMDL measure appears outside a table declaration: {source}"
            )

        measure_name = _unquote_tmdl_identifier(measure_match.group("name"))
        measure_indent = _indent_width(measure_match.group("indent"))
        rhs = measure_match.group("rhs").rstrip()
        continuation: list[str] = []
        cursor = index + 1
        while cursor < len(lines):
            child = lines[cursor]
            stripped = child.strip()
            child_indent = child[: len(child) - len(child.lstrip(" \t"))]
            if stripped and _indent_width(child_indent) <= measure_indent:
                break
            if stripped and _TMDL_MEASURE_PROPERTY.match(stripped):
                break
            continuation.append(child)
            cursor += 1

        continuation_text = textwrap.dedent("\n".join(continuation)).rstrip()
        dax_parts = [part for part in (rhs, continuation_text) if part]
        dax = "\n".join(dax_parts).rstrip()
        if "\x00" in dax:
            raise GroundedExplanationError(f"TMDL DAX contains a null byte: {source}")
        definitions.append(
            _MeasureDefinition(
                table=current_table,
                name=measure_name,
                dax=dax,
                source=source,
            )
        )
        index = cursor
    return definitions


def _structured_field_bindings(value: dict[str, Any], source: Path) -> set[_FieldBinding]:
    bindings: set[_FieldBinding] = set()
    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        for field_kind in ("Measure", "Column"):
            if field_kind not in node:
                continue
            field = node[field_kind]
            if not isinstance(field, dict):
                raise GroundedExplanationError(f"PBIR {field_kind} binding is malformed: {source}")
            expression = field.get("Expression")
            source_ref = expression.get("SourceRef") if isinstance(expression, dict) else None
            table = source_ref.get("Entity") if isinstance(source_ref, dict) else None
            property_name = field.get("Property")
            if not isinstance(table, str) or not isinstance(property_name, str):
                raise GroundedExplanationError(f"PBIR {field_kind} binding is malformed: {source}")
            bindings.add(
                _FieldBinding(
                    kind=field_kind.casefold(),
                    table=_safe_label(table, f"PBIR {field_kind} table"),
                    property_name=_safe_label(
                        property_name,
                        f"PBIR {field_kind} property",
                    ),
                )
            )
        stack.extend(node.values())
    return bindings


def _visual_title(visual: dict[str, Any], source: Path) -> str | None:
    containers = visual.get("visualContainerObjects")
    if containers is None:
        return None
    if not isinstance(containers, dict):
        raise GroundedExplanationError(f"PBIR visual container metadata is malformed: {source}")
    title_items = containers.get("title")
    if title_items is None:
        return None
    if not isinstance(title_items, list):
        raise GroundedExplanationError(f"PBIR visual title metadata is malformed: {source}")

    values: set[str] = set()
    for item in title_items:
        if not isinstance(item, dict):
            raise GroundedExplanationError(f"PBIR visual title metadata is malformed: {source}")
        properties = item.get("properties")
        text_value = properties.get("text") if isinstance(properties, dict) else None
        expression = text_value.get("expr") if isinstance(text_value, dict) else None
        literal = expression.get("Literal") if isinstance(expression, dict) else None
        raw_value = literal.get("Value") if isinstance(literal, dict) else None
        if raw_value is None:
            continue
        if not isinstance(raw_value, str):
            raise GroundedExplanationError(f"PBIR visual title metadata is malformed: {source}")
        decoded = _decode_powerbi_literal(raw_value)
        values.add(_safe_label(decoded, "PBIR visual title"))
    if len(values) > 1:
        raise GroundedExplanationError(f"PBIR visual has ambiguous stored titles: {source}")
    return next(iter(values), None)


def _decode_powerbi_literal(value: str) -> str:
    if len(value) >= 2 and value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def _bounded_files(
    root: Path,
    filename: str,
    *,
    maximum: int,
    label: str,
    recursive: bool,
    missing_ok: bool = False,
) -> list[Path]:
    if not root.exists():
        if missing_ok:
            return []
        raise GroundedExplanationError(f"{label} root does not exist: {root}")
    if _is_link_like(root) or not root.is_dir():
        raise GroundedExplanationError(f"{label} root is not a trusted directory: {root}")
    candidates = root.rglob(filename) if recursive else root.glob(f"*/{filename}")
    paths = sorted(candidates)
    if len(paths) > maximum:
        raise GroundedExplanationError(
            f"{label} inventory exceeds the supported maximum of {maximum} files."
        )
    root_resolved = root.resolve(strict=True)
    for path in paths:
        if _is_link_like(path) or not path.is_file():
            raise GroundedExplanationError(f"{label} metadata cannot use links: {path}")
        try:
            path.resolve(strict=True).relative_to(root_resolved)
        except (OSError, ValueError) as error:
            raise GroundedExplanationError(
                f"{label} metadata escapes its active artifact root: {path}"
            ) from error
    return paths


def _read_bounded_text(path: Path, label: str) -> str:
    try:
        size = path.stat().st_size
        if size > _MAX_METADATA_FILE_BYTES:
            raise GroundedExplanationError(
                f"{label} metadata exceeds {_MAX_METADATA_FILE_BYTES} bytes: {path}"
            )
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise GroundedExplanationError(f"{label} metadata cannot be read: {path}") from error


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    text = _read_bounded_text(path, label)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise GroundedExplanationError(
            f"{label} metadata must be a valid JSON object: {path}"
        ) from error
    if not isinstance(value, dict):
        raise GroundedExplanationError(f"{label} metadata must be a valid JSON object: {path}")
    return value


def _safe_label(value: str, label: str) -> str:
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > 200
        or any(character in normalized for character in "<>`[]{}")
        or any(ord(character) < 32 for character in normalized)
    ):
        raise GroundedExplanationError(f"{label} contains unsupported characters.")
    return normalized


def _unquote_tmdl_identifier(value: str, *, already_unquoted: bool = False) -> str:
    normalized = value.strip()
    if (
        not already_unquoted
        and len(normalized) >= 2
        and normalized.startswith("'")
        and normalized.endswith("'")
    ):
        normalized = normalized[1:-1].replace("''", "'")
    if not normalized or "\x00" in normalized or "\n" in normalized or "\r" in normalized:
        raise GroundedExplanationError("TMDL contains an invalid identifier.")
    return normalized


def _normalize_words(value: str) -> str:
    return " ".join(value.replace("_", " ").casefold().split())


def _phrase_pattern(value: str) -> str:
    words = [re.escape(word) for word in value.split()]
    return r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)"


def _indent_width(indent: str) -> int:
    return sum(4 if character == "\t" else 1 for character in indent)


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _indent_code(value: str) -> str:
    """Render untrusted metadata as an indented Markdown code block."""

    return "\n".join(f"    {line}" for line in value.splitlines())


def _markdown_text(value: str) -> str:
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", value)


def _casefold_reference(reference: str) -> tuple[str, str]:
    table, property_name = _parse_powerbi_reference(reference)
    return table.casefold(), property_name.casefold()
