"""Independent structural, semantic-binding, layout, and CLI validation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ai_powerbi_analyst.contracts import SemanticCatalog
from ai_powerbi_analyst.powerbi.cli import (
    AuthoringCliAdapter,
    CommandReceipt,
    PbiCliAdapter,
)
from ai_powerbi_analyst.powerbi.recipes import DashboardBlueprint, VisualBlueprint
from ai_powerbi_analyst.powerbi.theme import (
    EXECUTIVE_LIGHT_THEME_FILE,
    EXECUTIVE_LIGHT_THEME_SHA256,
    THEME_VERSION_AT_IMPORT,
)

_BLUEPRINT_ROLE_MAP: dict[str, dict[str, str]] = {
    "cardVisual": {"field": "Data", "value": "Data"},
    "lineChart": {"category": "Category", "value": "Y", "legend": "Series"},
    "clusteredBarChart": {"category": "Category", "value": "Y", "legend": "Series"},
    "pivotTable": {"row": "Rows", "value": "Values"},
    "slicer": {"field": "Values"},
    "textbox": {},
}


class ValidationSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    source: str
    severity: ValidationSeverity
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    source: str
    issues: tuple[ValidationIssue, ...]
    receipt_hashes: tuple[str, ...] = ()

    @property
    def errors(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is ValidationSeverity.ERROR)

    @property
    def warnings(self) -> tuple[ValidationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING)

    @property
    def passed(self) -> bool:
        # Validator warnings are not silently accepted. Some upstream warnings
        # can be false positives, but any exception must be an explicit policy
        # decision outside this default fail-closed boundary.
        return not self.errors and not self.warnings

    @classmethod
    def single(
        cls,
        *,
        source: str,
        severity: ValidationSeverity,
        code: str,
        message: str,
        path: str | None = None,
    ) -> ValidationReport:
        return cls(
            source=source,
            issues=(
                ValidationIssue(
                    source=source,
                    severity=severity,
                    code=code,
                    message=message,
                    path=path,
                ),
            ),
        )


def aggregate_validation(*reports: ValidationReport) -> ValidationReport:
    issues = tuple(issue for report in reports for issue in report.issues)
    receipt_hashes = tuple(
        receipt_hash for report in reports for receipt_hash in report.receipt_hashes
    )
    return ValidationReport(
        source="aggregate",
        issues=issues,
        receipt_hashes=receipt_hashes,
    )


def validate_blueprint_postconditions(
    report_path: Path,
    blueprint: DashboardBlueprint,
) -> ValidationReport:
    """Prove the staged PBIR contains the exact app-owned blueprint objects.

    Additional pages and visuals are deliberately allowed: they are outside the
    app's ownership boundary and must survive report and evidence updates.
    """

    issues: list[ValidationIssue] = []
    report_root = report_path.resolve()
    _validate_blueprint_theme(report_root, blueprint, issues)
    pages_root = report_root / "definition" / "pages"
    for page in blueprint.pages:
        page_root = pages_root / page.page_id
        page_path = page_root / "page.json"
        page_value = _load_object(page_path, issues)
        if page_value is None:
            continue
        expected_page = {
            "name": page.page_id,
            "displayName": page.title,
            "width": page.width,
            "height": page.height,
        }
        for key, expected in expected_page.items():
            if page_value.get(key) != expected:
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "BLUEPRINT_PAGE_MISMATCH",
                        f"Expected {key}={expected!r} for app-owned page {page.key}",
                        page_path,
                        source="blueprint-postcondition",
                    )
                )

        for visual in page.visuals:
            visual_path = page_root / "visuals" / visual.visual_id / "visual.json"
            value = _load_object(visual_path, issues)
            if value is None:
                continue
            # A full dashboard rebuild deliberately preserves the one governed
            # evidence replacement. The blank textbox is the recipe default;
            # once populated, only this exact approved variant may survive.
            if (
                visual.is_evidence_slot
                and visual.visual_type == "textbox"
                and is_approved_populated_evidence_slot(visual_path, visual)
            ):
                continue
            body = value.get("visual")
            if not isinstance(body, dict) or body.get("visualType") != visual.visual_type:
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "BLUEPRINT_VISUAL_TYPE_MISMATCH",
                        f"Visual {visual.logical_key} is not {visual.visual_type}",
                        visual_path,
                        source="blueprint-postcondition",
                    )
                )
            if isinstance(body, dict):
                expected_container = {
                    ("title", "text"): f"'{visual.title}'",
                    ("border", "show"): "true",
                    ("background", "show"): "true",
                }
                for (object_name, property_name), expected in expected_container.items():
                    actual = _visual_container_literal(
                        body,
                        object_name=object_name,
                        property_name=property_name,
                    )
                    if actual != expected:
                        issues.append(
                            _issue(
                                ValidationSeverity.ERROR,
                                "BLUEPRINT_VISUAL_CONTAINER_MISMATCH",
                                (
                                    f"Visual {visual.logical_key} must set "
                                    f"{object_name}.{property_name}={expected!r}"
                                ),
                                visual_path,
                                source="blueprint-postcondition",
                            )
                        )
            position = value.get("position")
            expected_position = {
                "x": visual.position.x,
                "y": visual.position.y,
                "width": visual.position.width,
                "height": visual.position.height,
            }
            if not isinstance(position, dict) or any(
                position.get(key) != expected for key, expected in expected_position.items()
            ):
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "BLUEPRINT_LAYOUT_MISMATCH",
                        f"Visual {visual.logical_key} does not match its deterministic bounds",
                        visual_path,
                        source="blueprint-postcondition",
                    )
                )
            role_map = _BLUEPRINT_ROLE_MAP.get(visual.visual_type, {})
            expected_bindings = sorted(
                (
                    role_map.get(binding.role, f"UNSUPPORTED:{binding.role}"),
                    "Measure" if binding.kind == "measure" else "Column",
                    _canonical_reference(binding.reference),
                )
                for binding in visual.bindings
            )
            actual_bindings = sorted(_extract_bindings(value))
            if actual_bindings != expected_bindings:
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "BLUEPRINT_BINDING_MISMATCH",
                        f"Visual {visual.logical_key} bindings do not match the approved recipe",
                        visual_path,
                        source="blueprint-postcondition",
                    )
                )
    return ValidationReport(source="blueprint-postcondition", issues=tuple(issues))


def is_approved_populated_evidence_slot(
    visual_path: Path,
    slot: VisualBlueprint,
) -> bool:
    """Return true only for the frozen conversational-evidence replacement.

    Report metadata is untrusted. Preservation therefore requires the stable
    slot identity, exact deterministic bounds, exact governed bindings, and
    exact container properties used by the approved recipe.
    """

    if not slot.is_evidence_slot or slot.visual_type != "textbox":
        return False
    try:
        value = json.loads(visual_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict) or value.get("name") != slot.visual_id:
        return False
    body = value.get("visual")
    if not isinstance(body, dict) or body.get("visualType") != "clusteredBarChart":
        return False
    expected_container = {
        ("title", "text"): "'West margin change · governed contributions'",
        ("border", "show"): "true",
        ("background", "show"): "true",
    }
    if any(
        _visual_container_literal(
            body,
            object_name=object_name,
            property_name=property_name,
        )
        != expected
        for (object_name, property_name), expected in expected_container.items()
    ):
        return False
    position = value.get("position")
    expected_position = {
        "x": slot.position.x,
        "y": slot.position.y,
        "width": slot.position.width,
        "height": slot.position.height,
    }
    if not isinstance(position, dict) or any(
        position.get(key) != expected for key, expected in expected_position.items()
    ):
        return False
    expected_bindings = sorted(
        (
            ("Category", "Column", "Driver Contributions[Driver]"),
            (
                "Y",
                "Measure",
                "Driver Contributions[Driver Contribution — West]",
            ),
        )
    )
    return sorted(_extract_bindings(value)) == expected_bindings


def _validate_blueprint_theme(
    report_root: Path,
    blueprint: DashboardBlueprint,
    issues: list[ValidationIssue],
) -> None:
    report_path = report_root / "definition" / "report.json"
    if blueprint.style_preset != "executive_light":
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_PRESET_UNSUPPORTED",
                f"Unsupported report style preset: {blueprint.style_preset}",
                report_path,
                source="blueprint-postcondition",
            )
        )
        return
    report = _load_object(report_path, issues)
    if report is None:
        return
    collection = report.get("themeCollection")
    custom_theme = collection.get("customTheme") if isinstance(collection, dict) else None
    if not isinstance(custom_theme, dict) or (
        custom_theme.get("name") != EXECUTIVE_LIGHT_THEME_FILE
        or custom_theme.get("type") != "RegisteredResources"
        or custom_theme.get("reportVersionAtImport") != THEME_VERSION_AT_IMPORT
    ):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_REGISTRATION_MISMATCH",
                "The frozen executive-light theme is not registered as the custom theme",
                report_path,
                source="blueprint-postcondition",
            )
        )

    resource_packages = report.get("resourcePackages")
    registered_items: list[object] = []
    if isinstance(resource_packages, list):
        for package in resource_packages:
            if isinstance(package, dict) and package.get("name") == "RegisteredResources":
                items = package.get("items")
                if isinstance(items, list):
                    registered_items.extend(items)
    expected_item = {
        "name": EXECUTIVE_LIGHT_THEME_FILE,
        "type": "CustomTheme",
        "path": EXECUTIVE_LIGHT_THEME_FILE,
    }
    matching_items = [item for item in registered_items if item == expected_item]
    if len(matching_items) != 1:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_RESOURCE_MISMATCH",
                "The frozen executive-light theme resource registration is missing or duplicated",
                report_path,
                source="blueprint-postcondition",
            )
        )

    theme_path = (
        report_root / "StaticResources" / "RegisteredResources" / EXECUTIVE_LIGHT_THEME_FILE
    )
    if not theme_path.is_file():
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_FILE_MISSING",
                "The registered executive-light theme file is missing",
                theme_path,
                source="blueprint-postcondition",
            )
        )
        return
    try:
        digest = hashlib.sha256(theme_path.read_bytes()).hexdigest()
    except OSError as error:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_FILE_UNREADABLE",
                f"The registered executive-light theme could not be read: {error}",
                theme_path,
                source="blueprint-postcondition",
            )
        )
        return
    if digest != EXECUTIVE_LIGHT_THEME_SHA256:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "BLUEPRINT_THEME_HASH_MISMATCH",
                "The registered executive-light theme does not match the frozen release hash",
                theme_path,
                source="blueprint-postcondition",
            )
        )


def _visual_container_literal(
    visual_body: dict[str, object],
    *,
    object_name: str,
    property_name: str,
) -> object:
    container = visual_body.get("visualContainerObjects")
    entries = container.get(object_name) if isinstance(container, dict) else None
    entry = entries[0] if isinstance(entries, list) and entries else None
    properties = entry.get("properties") if isinstance(entry, dict) else None
    property_value = properties.get(property_name) if isinstance(properties, dict) else None
    expression = property_value.get("expr") if isinstance(property_value, dict) else None
    literal = expression.get("Literal") if isinstance(expression, dict) else None
    return literal.get("Value") if isinstance(literal, dict) else None


def validate_semantic_catalog_tmdl(
    semantic_model_path: Path,
    catalog: SemanticCatalog,
) -> ValidationReport:
    """Audit every catalog binding against the active TMDL object declarations."""

    model = semantic_model_path.resolve()
    definition = model / "definition"
    issues: list[ValidationIssue] = []
    if not definition.is_dir():
        return ValidationReport.single(
            source="semantic-model",
            severity=ValidationSeverity.ERROR,
            code="TMDL_DEFINITION_MISSING",
            message="The active semantic model definition directory is missing",
            path=str(definition),
        )

    declarations: dict[str, list[tuple[str, bool, Path]]] = {}
    table_expression = re.compile(r"^\s*table\s+(?:'((?:''|[^'])+)'|([^\s=]+))\s*$")
    member_expression = re.compile(
        r"^\s*(column|measure)\s+(?:'((?:''|[^'])+)'|([^\s=]+))(?=\s*=|\s*$)"
    )
    for path in sorted(definition.rglob("*.tmdl")):
        current_table: str | None = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as error:
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "TMDL_READ_FAILED",
                    f"Could not read TMDL: {error}",
                    path,
                    source="semantic-model",
                )
            )
            continue
        for line_index, line in enumerate(lines):
            table_match = table_expression.match(line)
            if table_match:
                current_table = (table_match.group(1) or table_match.group(2)).replace("''", "'")
                continue
            member_match = member_expression.match(line)
            if member_match and current_table is not None:
                kind = member_match.group(1)
                member = (member_match.group(2) or member_match.group(3)).replace("''", "'")
                reference = f"{current_table}[{member}]"
                has_formula = kind == "column" or _tmdl_measure_has_formula(lines, line_index)
                declarations.setdefault(reference, []).append((kind, has_formula, path))

    expected = {
        **{_canonical_reference(metric.powerbi_ref): "measure" for metric in catalog.metrics},
        **{
            _canonical_reference(dimension.powerbi_ref): "column"
            for dimension in catalog.dimensions
        },
        **{
            _canonical_reference(report_object.powerbi_ref): report_object.kind
            for report_object in catalog.report_objects
        },
    }
    for reference, expected_kind in sorted(expected.items()):
        candidates = declarations.get(reference, [])
        if not candidates:
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "TMDL_BINDING_MISSING",
                    f"Catalog binding does not exist in the active semantic model: {reference}",
                    definition,
                    source="semantic-model",
                )
            )
            continue
        matching = [item for item in candidates if item[0] == expected_kind]
        if not matching:
            actual = ", ".join(sorted({item[0] for item in candidates}))
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "TMDL_BINDING_KIND_MISMATCH",
                    f"Catalog expects {reference} to be a {expected_kind}; found {actual}",
                    candidates[0][2],
                    source="semantic-model",
                )
            )
        elif expected_kind == "measure" and not any(item[1] for item in matching):
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "TMDL_MEASURE_FORMULA_MISSING",
                    f"Catalog measure has no DAX formula: {reference}",
                    matching[0][2],
                    source="semantic-model",
                )
            )
    return ValidationReport(source="semantic-model", issues=tuple(issues))


def _tmdl_measure_has_formula(lines: list[str], line_index: int) -> bool:
    declaration = lines[line_index]
    _, separator, inline = declaration.partition("=")
    if separator and inline.strip():
        return True
    if not separator:
        return False
    for candidate in lines[line_index + 1 :]:
        stripped = candidate.strip()
        if not stripped:
            continue
        if re.match(r"^(?:column|measure|partition|table)\b", stripped):
            return False
        return not bool(
            re.match(
                r"^(?:formatString|displayFolder|description|isHidden|annotation)\b",
                stripped,
            )
        )
    return False


def validate_report(
    report_path: Path,
    *,
    catalog: SemanticCatalog | None = None,
) -> ValidationReport:
    """Validate the local PBIR subset needed by approved report recipes."""

    report = report_path.resolve()
    issues: list[ValidationIssue] = []
    if not report.is_dir():
        return ValidationReport.single(
            source="local",
            severity=ValidationSeverity.ERROR,
            code="PBIR_REPORT_MISSING",
            message="Report directory does not exist",
            path=str(report),
        )
    for path in report.rglob("*"):
        if path.is_symlink():
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "PBIR_SYMLINK_FORBIDDEN",
                    "Symbolic links are forbidden inside a report transaction",
                    path,
                )
            )

    pages_root = report / "definition" / "pages"
    metadata_path = pages_root / "pages.json"
    metadata = _load_object(metadata_path, issues)
    if metadata is None:
        return ValidationReport(source="local", issues=tuple(issues))

    page_order_value = metadata.get("pageOrder")
    if not isinstance(page_order_value, list) or not all(
        isinstance(value, str) for value in page_order_value
    ):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_PAGE_ORDER_INVALID",
                "pageOrder must be an array of page IDs",
                metadata_path,
            )
        )
        page_order: list[str] = []
    else:
        page_order = list(page_order_value)
        if len(page_order) != len(set(page_order)):
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "PBIR_PAGE_ORDER_DUPLICATE",
                    "pageOrder contains duplicate page IDs",
                    metadata_path,
                )
            )

    active = metadata.get("activePageName")
    if page_order and active not in page_order:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_ACTIVE_PAGE_UNKNOWN",
                "activePageName must identify a page in pageOrder",
                metadata_path,
            )
        )

    actual_page_ids = {
        path.name for path in pages_root.iterdir() if path.is_dir() and not path.is_symlink()
    }
    listed_page_ids = set(page_order)
    for missing in sorted(listed_page_ids.difference(actual_page_ids)):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_PAGE_MISSING",
                f"pageOrder references missing page {missing}",
                pages_root / missing,
            )
        )
    for unlisted in sorted(actual_page_ids.difference(listed_page_ids)):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_PAGE_UNLISTED",
                f"Page directory {unlisted} is absent from pageOrder",
                pages_root / unlisted,
            )
        )

    known_references: set[str] | None = None
    if catalog is not None:
        known_references = {
            *(_canonical_reference(metric.powerbi_ref) for metric in catalog.metrics),
            *(_canonical_reference(dimension.powerbi_ref) for dimension in catalog.dimensions),
            *(
                _canonical_reference(report_object.powerbi_ref)
                for report_object in catalog.report_objects
            ),
        }

    seen_visual_ids: set[str] = set()
    for page_id in page_order:
        page_root = pages_root / page_id
        if not page_root.is_dir():
            continue
        page_path = page_root / "page.json"
        page = _load_object(page_path, issues)
        if page is None:
            continue
        if page.get("name") != page_id:
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "PBIR_PAGE_NAME_MISMATCH",
                    "Page name must match its directory ID",
                    page_path,
                )
            )
        width = _positive_number(page.get("width"))
        height = _positive_number(page.get("height"))
        if width is None or height is None:
            issues.append(
                _issue(
                    ValidationSeverity.ERROR,
                    "PBIR_PAGE_SIZE_INVALID",
                    "Page width and height must be finite positive numbers",
                    page_path,
                )
            )
            continue

        visuals_root = page_root / "visuals"
        if not visuals_root.exists():
            continue
        for visual_root in sorted(path for path in visuals_root.iterdir() if path.is_dir()):
            visual_path = visual_root / "visual.json"
            visual = _load_object(visual_path, issues)
            if visual is None:
                continue
            visual_id = visual_root.name
            if visual.get("name") != visual_id:
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "PBIR_VISUAL_NAME_MISMATCH",
                        "Visual name must match its directory ID",
                        visual_path,
                    )
                )
            if visual_id in seen_visual_ids:
                issues.append(
                    _issue(
                        ValidationSeverity.ERROR,
                        "PBIR_VISUAL_ID_DUPLICATE",
                        f"Visual ID {visual_id} is not report-unique",
                        visual_path,
                    )
                )
            seen_visual_ids.add(visual_id)
            _validate_position(visual.get("position"), width, height, visual_path, issues)
            if known_references is not None:
                for reference in sorted(_extract_references(visual)):
                    if reference not in known_references:
                        issues.append(
                            _issue(
                                ValidationSeverity.ERROR,
                                "PBIR_BINDING_UNKNOWN",
                                f"Binding does not resolve in the semantic catalog: {reference}",
                                visual_path,
                            )
                        )
    return ValidationReport(source="local", issues=tuple(issues))


def validate_with_tools(
    report_path: Path,
    *,
    catalog: SemanticCatalog,
    pbi_cli: PbiCliAdapter | None,
    authoring_cli: AuthoringCliAdapter | None,
) -> ValidationReport:
    reports = [validate_report(report_path, catalog=catalog)]
    if pbi_cli is None:
        reports.append(
            ValidationReport.single(
                source="pbi-cli",
                severity=ValidationSeverity.ERROR,
                code="PBI_CLI_MISSING",
                message="pbi-cli validation is required before promotion",
            )
        )
    else:
        reports.append(
            validation_from_receipt(
                "pbi-cli",
                pbi_cli.validate(report_path),
                expected_report_path=report_path,
            )
        )
    if authoring_cli is None:
        reports.append(
            ValidationReport.single(
                source="microsoft-authoring-cli",
                severity=ValidationSeverity.ERROR,
                code="AUTHORING_CLI_MISSING",
                message="Microsoft report-authoring validation is required before promotion",
            )
        )
    else:
        reports.append(
            validation_from_receipt(
                "microsoft-authoring-cli",
                authoring_cli.validate(report_path),
                expected_report_path=report_path,
            )
        )
    return aggregate_validation(*reports)


def make_external_validator(
    pbi_cli: PbiCliAdapter,
    authoring_cli: AuthoringCliAdapter,
    *,
    receipt_root: Path | None = None,
) -> Callable[[Path, SemanticCatalog], ValidationReport]:
    """Create the independent production validator expected by SafePowerBIComposer."""

    def validator(report_path: Path, _catalog: SemanticCatalog) -> ValidationReport:
        pbi_receipt = pbi_cli.validate(report_path)
        authoring_receipt = authoring_cli.validate(report_path)
        pbi_report = validation_from_receipt(
            "pbi-cli", pbi_receipt, expected_report_path=report_path
        )
        authoring_report = validation_from_receipt(
            "microsoft-authoring-cli",
            authoring_receipt,
            expected_report_path=report_path,
        )
        if receipt_root is not None:
            pbi_report = _attach_receipt(
                pbi_report,
                pbi_receipt,
                receipt_root,
                "pbi-cli",
            )
            authoring_report = _attach_receipt(
                authoring_report,
                authoring_receipt,
                receipt_root,
                "microsoft-authoring-cli",
            )
        return aggregate_validation(
            pbi_report,
            authoring_report,
        )

    return validator


def validation_from_receipt(
    source: str,
    receipt: CommandReceipt,
    *,
    expected_report_path: Path | None = None,
) -> ValidationReport:
    if not receipt.succeeded:
        detail = (receipt.stderr or receipt.stdout).strip()
        return ValidationReport.single(
            source=source,
            severity=ValidationSeverity.ERROR,
            code="CLI_VALIDATION_FAILED",
            message=detail[:1000] or f"Validator exited with code {receipt.returncode}",
        )
    if not isinstance(receipt.parsed_json, (dict, list)):
        detail = (receipt.stdout or receipt.stderr).strip()
        return ValidationReport.single(
            source=source,
            severity=ValidationSeverity.ERROR,
            code="CLI_VALIDATION_CONTRACT_INVALID",
            message=(
                "Validator succeeded without the required JSON object/array contract"
                + (f": {detail[:800]}" if detail else "")
            ),
        )
    report_target, invocation_error = _validation_invocation_contract_error(
        source,
        receipt,
        expected_report_path=expected_report_path,
    )
    contract_error = invocation_error or _validation_contract_error(
        source,
        receipt.parsed_json,
        report_target=report_target,
    )
    if contract_error is not None:
        return ValidationReport.single(
            source=source,
            severity=ValidationSeverity.ERROR,
            code="CLI_VALIDATION_CONTRACT_INVALID",
            message=contract_error,
        )
    issues: list[ValidationIssue] = []
    _collect_cli_issues(receipt.parsed_json, source, issues)
    return ValidationReport(source=source, issues=tuple(issues))


def _validation_invocation_contract_error(
    source: str,
    receipt: CommandReceipt,
    *,
    expected_report_path: Path | None,
) -> tuple[Path | None, str | None]:
    argv = receipt.argv
    if source == "pbi-cli":
        allowed_names = {"pbi", "pbi.exe", "pbi.cmd", "pbi.bat"}
        expected_tail = ("--json", "report", "--path")
        if (
            len(argv) != 7
            or Path(argv[0]).name.casefold() not in allowed_names
            or argv[1:4] != expected_tail
            or not argv[4]
            or argv[5:] != ("validate", "--full")
        ):
            return None, "pbi-cli receipt does not match the frozen validation argv"
        report_target = Path(argv[4]).resolve(strict=False)
    elif source == "microsoft-authoring-cli":
        allowed_names = {
            "powerbi-report-author",
            "powerbi-report-author.exe",
            "powerbi-report-author.cmd",
            "powerbi-report-author.bat",
        }
        if (
            len(argv) != 3
            or Path(argv[0]).name.casefold() not in allowed_names
            or argv[1] != "validate"
            or not argv[2]
        ):
            return None, (
                "Microsoft authoring CLI receipt does not match the frozen validation argv"
            )
        report_target = Path(argv[2]).resolve(strict=False)
    else:
        return None, f"No frozen validation argv contract exists for source {source!r}"
    if expected_report_path is not None and report_target != expected_report_path.resolve(
        strict=False
    ):
        return None, "Validator receipt targets a different report than the staged report"
    return report_target, None


def _validation_contract_error(
    source: str,
    payload: object,
    *,
    report_target: Path | None,
) -> str | None:
    if not isinstance(payload, dict):
        return "Validator JSON must be an object using the pinned success envelope"
    if source == "pbi-cli":
        summary = payload.get("summary")
        errors = payload.get("errors")
        warnings = payload.get("warnings")
        info = payload.get("info")
        if (
            not isinstance(payload.get("valid"), bool)
            or not isinstance(errors, list)
            or not isinstance(warnings, list)
            or not isinstance(info, list)
            or not isinstance(summary, dict)
            or not all(
                isinstance(summary.get(key), int)
                and not isinstance(summary.get(key), bool)
                and summary[key] >= 0
                for key in ("errors", "warnings", "info")
            )
            or summary["errors"] != len(errors)
            or summary["warnings"] != len(warnings)
            or summary["info"] != len(info)
        ):
            return "pbi-cli did not return its pinned validation JSON envelope"
        return None
    if source == "microsoft-authoring-cli":
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or data.get("result") not in {"succeeded", "succeededWithWarnings", "failed"}
            or not isinstance(data.get("errorCount"), int)
            or isinstance(data.get("errorCount"), bool)
            or data["errorCount"] < 0
            or not isinstance(data.get("warningCount"), int)
            or isinstance(data.get("warningCount"), bool)
            or data["warningCount"] < 0
            or not isinstance(data.get("reportPath"), str)
            or not data["reportPath"]
        ):
            return "Microsoft authoring CLI did not return its pinned validation JSON envelope"
        assert isinstance(data, dict)
        reported_path = Path(str(data["reportPath"])).resolve(strict=False)
        if report_target is None or reported_path != report_target:
            return "Microsoft authoring CLI JSON names a different report than its argv"
        return None
    return f"No frozen validation JSON contract exists for source {source!r}"


def _attach_receipt(
    report: ValidationReport,
    receipt: CommandReceipt,
    receipt_root: Path,
    source: str,
) -> ValidationReport:
    payload: dict[str, object] = {
        "source": source,
        "argv": list(receipt.argv),
        "cwd": str(receipt.cwd) if receipt.cwd is not None else None,
        "returncode": receipt.returncode,
        "stdout": receipt.stdout,
        "stderr": receipt.stderr,
        "parsed_json": receipt.parsed_json,
        "duration_ms": receipt.duration_ms,
    }
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    receipt_root.mkdir(parents=True, exist_ok=True)
    destination = receipt_root / f"{source}-{digest}.json"
    if not destination.exists():
        temporary = destination.with_suffix(".json.tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, destination)
    return ValidationReport(
        source=report.source,
        issues=report.issues,
        receipt_hashes=(*report.receipt_hashes, digest),
    )


def _collect_cli_issues(
    value: object,
    source: str,
    issues: list[ValidationIssue],
    *,
    inherited_severity: ValidationSeverity | None = None,
) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_cli_issues(item, source, issues, inherited_severity=inherited_severity)
        return
    if not isinstance(value, dict):
        return

    severity = inherited_severity
    raw_severity = value.get("severity") or value.get("level")
    if isinstance(raw_severity, str):
        normalized = raw_severity.casefold()
        if normalized in {"error", "fatal"}:
            severity = ValidationSeverity.ERROR
        elif normalized in {"warning", "warn"}:
            severity = ValidationSeverity.WARNING
        elif normalized == "info":
            severity = ValidationSeverity.INFO

    message = value.get("message") or value.get("description")
    if severity is not None and isinstance(message, str):
        code = value.get("code") or value.get("ruleId") or "CLI_VALIDATION_ISSUE"
        path = value.get("path") or value.get("file")
        issues.append(
            ValidationIssue(
                source=source,
                severity=severity,
                code=str(code),
                message=message,
                path=str(path) if path is not None else None,
            )
        )

    if value.get("valid") is False and not any(
        issue.source == source and issue.severity is ValidationSeverity.ERROR for issue in issues
    ):
        issues.append(
            ValidationIssue(
                source=source,
                severity=ValidationSeverity.ERROR,
                code="CLI_REPORTED_INVALID",
                message="Validator reported valid=false",
            )
        )

    for key, child in value.items():
        normalized_key = key.casefold().replace("_", "")
        if normalized_key == "result" and isinstance(child, str):
            normalized_result = child.casefold()
            if normalized_result == "succeededwithwarnings":
                issues.append(
                    ValidationIssue(
                        source=source,
                        severity=ValidationSeverity.WARNING,
                        code="CLI_SUCCEEDED_WITH_WARNINGS",
                        message="Validator reported success with warnings",
                    )
                )
            elif normalized_result == "failed":
                issues.append(
                    ValidationIssue(
                        source=source,
                        severity=ValidationSeverity.ERROR,
                        code="CLI_REPORTED_FAILURE",
                        message="Validator reported a failed result",
                    )
                )
        if normalized_key in {"succeededwithwarnings", "successwithwarnings"} and (
            child is True
            or (isinstance(child, int) and not isinstance(child, bool) and child > 0)
            or (isinstance(child, str) and child.strip().casefold() not in {"", "0", "false"})
        ):
            issues.append(
                ValidationIssue(
                    source=source,
                    severity=ValidationSeverity.WARNING,
                    code="CLI_SUCCEEDED_WITH_WARNINGS",
                    message="Validator reported success with warnings",
                )
            )
        if (
            normalized_key in {"warnings", "warningcount"}
            and isinstance(child, int)
            and not isinstance(child, bool)
            and child > 0
        ):
            issues.append(
                ValidationIssue(
                    source=source,
                    severity=ValidationSeverity.WARNING,
                    code="CLI_WARNING_COUNT",
                    message=f"Validator reported {child} warning(s) without structured details",
                )
            )
        if (
            normalized_key in {"errors", "errorcount", "failures", "failurecount"}
            and isinstance(child, int)
            and not isinstance(child, bool)
            and child > 0
        ):
            issues.append(
                ValidationIssue(
                    source=source,
                    severity=ValidationSeverity.ERROR,
                    code="CLI_ERROR_COUNT",
                    message=f"Validator reported {child} error(s) without structured details",
                )
            )
        child_severity = severity
        if key.casefold() in {"errors", "failures"}:
            child_severity = ValidationSeverity.ERROR
        elif key.casefold() in {"warnings"}:
            child_severity = ValidationSeverity.WARNING
        _collect_cli_issues(child, source, issues, inherited_severity=child_severity)


def _load_object(path: Path, issues: list[ValidationIssue]) -> dict[str, object] | None:
    if not path.is_file():
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_JSON_MISSING",
                "Required JSON file is missing",
                path,
            )
        )
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_JSON_INVALID",
                f"Could not parse JSON: {exc}",
                path,
            )
        )
        return None
    if not isinstance(value, dict):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_JSON_NOT_OBJECT",
                "PBIR JSON root must be an object",
                path,
            )
        )
        return None
    return value


def _validate_position(
    position_value: object,
    page_width: float,
    page_height: float,
    path: Path,
    issues: list[ValidationIssue],
) -> None:
    if not isinstance(position_value, dict):
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_LAYOUT_POSITION_MISSING",
                "Visual position must be an object",
                path,
            )
        )
        return
    x = _nonnegative_number(position_value.get("x"))
    y = _nonnegative_number(position_value.get("y"))
    width = _positive_number(position_value.get("width"))
    height = _positive_number(position_value.get("height"))
    if x is None or y is None or width is None or height is None:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_LAYOUT_POSITION_INVALID",
                "Visual coordinates must be finite; size must be positive",
                path,
            )
        )
        return
    if x + width > page_width or y + height > page_height:
        issues.append(
            _issue(
                ValidationSeverity.ERROR,
                "PBIR_LAYOUT_BOUNDS",
                "Visual extends beyond the page canvas",
                path,
            )
        )


def _positive_number(value: object) -> float | None:
    number = _finite_number(value)
    if number is None or number <= 0:
        return None
    return number


def _nonnegative_number(value: object) -> float | None:
    number = _finite_number(value)
    if number is None or number < 0:
        return None
    return number


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _extract_references(value: object) -> set[str]:
    references: set[str] = set()

    def visit(item: object) -> None:
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if not isinstance(item, dict):
            return
        for semantic_kind in ("Measure", "Column"):
            semantic = item.get(semantic_kind)
            if not isinstance(semantic, dict):
                continue
            expression = semantic.get("Expression")
            source_ref = expression.get("SourceRef") if isinstance(expression, dict) else None
            entity = source_ref.get("Entity") if isinstance(source_ref, dict) else None
            prop = semantic.get("Property")
            if isinstance(entity, str) and isinstance(prop, str):
                references.add(_canonical_reference(f"{entity}[{prop}]"))
        for child in item.values():
            visit(child)

    visit(value)
    return references


def _extract_bindings(value: object) -> list[tuple[str, str, str]]:
    if not isinstance(value, dict):
        return []
    visual = value.get("visual")
    query = visual.get("query") if isinstance(visual, dict) else None
    query_state = query.get("queryState") if isinstance(query, dict) else None
    if not isinstance(query_state, dict):
        return []
    bindings: list[tuple[str, str, str]] = []
    for role, role_value in query_state.items():
        if not isinstance(role, str) or not isinstance(role_value, dict):
            continue
        projections = role_value.get("projections")
        if not isinstance(projections, list):
            continue
        for projection in projections:
            field = projection.get("field") if isinstance(projection, dict) else None
            if not isinstance(field, dict):
                continue
            for semantic_kind in ("Measure", "Column"):
                semantic = field.get(semantic_kind)
                if not isinstance(semantic, dict):
                    continue
                expression = semantic.get("Expression")
                source_ref = expression.get("SourceRef") if isinstance(expression, dict) else None
                entity = source_ref.get("Entity") if isinstance(source_ref, dict) else None
                prop = semantic.get("Property")
                if isinstance(entity, str) and isinstance(prop, str):
                    bindings.append(
                        (
                            role,
                            semantic_kind,
                            _canonical_reference(f"{entity}[{prop}]"),
                        )
                    )
    return bindings


def _canonical_reference(reference: str) -> str:
    if not reference.endswith("]") or "[" not in reference:
        return reference.strip()
    table, member = reference[:-1].split("[", 1)
    normalized_table = table.strip().strip("'").replace("''", "'")
    return f"{normalized_table}[{member}]"


def _issue(
    severity: ValidationSeverity,
    code: str,
    message: str,
    path: Path,
    *,
    source: str = "local",
) -> ValidationIssue:
    return ValidationIssue(
        source=source,
        severity=severity,
        code=code,
        message=message,
        path=str(path),
    )
