"""Deterministic TMDL/PBIR project generation for reviewed user data."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from pathlib import Path

from filelock import FileLock

from ai_powerbi_analyst.contracts import (
    ColumnProfile,
    DashboardPlan,
    DslMeasureSpec,
    ProposedDaxMeasure,
    SemanticTablePlan,
    SourceFile,
    SourceKind,
    SourceManifest,
    TableProfile,
    ValidationReceipt,
    VisualPlan,
    VisualRoleBinding,
)
from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.powerbi.calendar_table import (
    MONTH_START_COLUMN,
    daily_date_key,
    plan_calendar_table,
)
from ai_powerbi_analyst.powerbi.dax_validation import dax_argument_errors, unsupported_dax_functions
from ai_powerbi_analyst.powerbi.theme import register_studio_theme
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.powerbi.visual_filters import date_filter_config
from ai_powerbi_analyst.powerbi.visual_forecast import visual_forecast_objects
from ai_powerbi_analyst.preflight import assert_powerbi_desktop_closed, probe_pbi_cli, probe_tool

_VISUAL_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "visualContainer/2.7.0/schema.json"
)
_PAGE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/"
    "page/2.1.0/schema.json"
)


class SemanticProjectError(RuntimeError):
    """A reviewed semantic project could not be compiled or validated."""


def validate_desktop_paths(project: Path, *, destination_root: Path | None = None) -> None:
    """Desktop still rejects long PBIR paths even if Python supports them."""
    destination = destination_root or project
    for path in project.rglob("*"):
        target = destination / path.relative_to(project)
        # Desktop's Windows UTF-16 path limit includes the terminating NUL.
        units = len(str(target.resolve(strict=False)).encode("utf-16-le")) // 2
        if units >= (248 if path.is_dir() else 260):
            raise SemanticProjectError(
                "The Power BI project path is too long for Desktop. "
                "Choose a shorter workspace or export folder before building/opening."
            )


class SemanticProjectBuilder:
    def __init__(
        self,
        projects_root: str | Path,
        template_root: str | Path,
        *,
        desktop_closed_check: Callable[[], None] = assert_powerbi_desktop_closed,
        require_external_validators: bool = True,
    ) -> None:
        self.projects_root = Path(projects_root).expanduser().resolve(strict=False)
        self.template_root = Path(template_root).expanduser().resolve(strict=True)
        self.backup_root = self.projects_root / ".backups"
        self.evidence_root = self.projects_root / ".evidence"
        self.undo_root = self.projects_root / ".undo"
        self.desktop_closed_check = desktop_closed_check
        self.require_external_validators = require_external_validators

    def build(
        self,
        name: str,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
        *,
        project_key: str | None = None,
    ) -> tuple[Path, ValidationReceipt]:
        self.projects_root.mkdir(parents=True, exist_ok=True)
        with FileLock(self.projects_root / ".studio-operation.lock", timeout=0):
            return self._build_locked(name, manifest, dashboard, project_key=project_key)

    def _build_locked(
        self,
        name: str,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
        *,
        project_key: str | None = None,
    ) -> tuple[Path, ValidationReceipt]:
        self.desktop_closed_check()
        if not dashboard.approved or not dashboard.semantic_model.approved:
            raise SemanticProjectError("Approve the semantic model and dashboard plan first")
        if dashboard.semantic_model.manifest_import_id != manifest.import_id:
            raise SemanticProjectError("Dashboard plan was reviewed against another source import")
        unresolved = [
            relationship.id
            for relationship in dashboard.semantic_model.relationships
            if relationship.review_status != "approved" or relationship.blockers
        ]
        if unresolved:
            raise SemanticProjectError("Dashboard contains unapproved relationships")
        _semantic_table_plan_index(manifest, dashboard)
        _validate_measure_graph(manifest, dashboard.semantic_model.measures)
        project_id = _identifier(project_key or name)
        target = self.projects_root / project_id
        original_hash = hash_tree(target) if target.exists() else None
        self.projects_root.mkdir(parents=True, exist_ok=True)
        stage = self.projects_root / f".{project_id}.stage-{uuid.uuid4().hex}"
        backup: Path | None = None
        promoted_target = False
        evidence = self.evidence_root / f"{project_id}-validation-receipt.json"
        marker = self.undo_root / f"{project_id}.json"
        metadata_before = {
            path: path.read_bytes() if path.is_file() else None for path in (evidence, marker)
        }
        try:
            self._write_project(stage, name, manifest, dashboard, project_key=project_id)
            validate_desktop_paths(stage, destination_root=target)
            receipt = self.validate(stage, manifest, dashboard)
            if not receipt.passed:
                raise SemanticProjectError(
                    "Generated PBIP failed local validation: " + "; ".join(receipt.issues)
                )
            current_hash = hash_tree(target) if target.exists() else None
            if current_hash != original_hash:
                raise SemanticProjectError("Power BI project changed while the build was staged")
            if target.exists():
                self.backup_root.mkdir(parents=True, exist_ok=True)
                backup = self.backup_root / f"{project_id}-{uuid.uuid4().hex}"
                os.replace(target, backup)
            os.replace(stage, target)
            promoted_target = True
            promoted = receipt.model_copy(update={"project_hash": hash_tree(target)})
            self.evidence_root.mkdir(parents=True, exist_ok=True)
            evidence.write_text(promoted.model_dump_json(indent=2) + "\n", encoding="utf-8")
            if backup is not None:
                self.undo_root.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    json.dumps({"backup_path": str(backup)}, indent=2) + "\n",
                    encoding="utf-8",
                )
            return target / f"{project_id}.pbip", promoted
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            if promoted_target and target.exists():
                # Keep failed output for diagnosis while restoring the previous exact bytes.
                failed = self.projects_root / f".{project_id}.failed-{uuid.uuid4().hex}"
                os.replace(target, failed)
            if backup is not None and backup.exists() and not target.exists():
                os.replace(backup, target)
            if promoted_target:
                for path, content in metadata_before.items():
                    if content is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(content)
            raise

    def undo(self, pbip_path: str | Path) -> bool:
        project = Path(pbip_path).expanduser().resolve(strict=True).parent
        marker = self.undo_root / f"{project.name}.json"
        if not marker.is_file():
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
        backup = Path(str(value.get("backup_path", ""))).resolve(strict=True)
        if not backup.is_relative_to(self.backup_root.resolve()):
            raise SemanticProjectError("Undo backup escapes the workspace")
        displaced = self.backup_root / f"failed-{uuid.uuid4().hex}"
        os.replace(project, displaced)
        try:
            os.replace(backup, project)
        except Exception:
            os.replace(displaced, project)
            raise
        return True

    def relink(
        self,
        pbip_path: str | Path,
        manifest: SourceManifest,
    ) -> ValidationReceipt:
        self.desktop_closed_check()
        project = Path(pbip_path).expanduser().resolve(strict=True).parent
        if not project.is_relative_to(self.projects_root.resolve()):
            raise SemanticProjectError("Power BI project escapes the managed workspace")
        missing = [source.original_name for source in manifest.sources if not source.original_path]
        if missing:
            raise SemanticProjectError(
                "Every source needs a reviewed original path before relinking"
            )
        relinked_sources: list[SourceFile] = []
        for source in manifest.sources:
            assert source.original_path is not None
            original = Path(source.original_path).resolve(strict=True)
            if source.original_sha256 is None or source.original_kind is None:
                raise SemanticProjectError(
                    f"Original source provenance is incomplete: {source.original_name}"
                )
            if not original.is_file() or sha256_file(original) != source.original_sha256:
                raise SemanticProjectError(
                    f"Original source differs from its reviewed snapshot: {source.original_name}"
                )
            relinked_sources.append(
                source.model_copy(
                    update={
                        "snapshot_path": str(original),
                        "sha256": source.original_sha256,
                        "size_bytes": original.stat().st_size,
                        "kind": source.original_kind,
                    }
                )
            )
        relinked_manifest = manifest.model_copy(update={"sources": relinked_sources})
        evidence_root = project / ".ai-powerbi"
        try:
            dashboard = DashboardPlan.model_validate_json(
                (evidence_root / "dashboard-plan.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as error:
            raise SemanticProjectError("Managed dashboard-plan evidence is missing") from error
        stage = self.projects_root / f".{project.name}.relink-{uuid.uuid4().hex}"
        backup = self.backup_root / f"{project.name}-{uuid.uuid4().hex}"
        shutil.copytree(project, stage)
        try:
            model_candidates = list(stage.glob("*.SemanticModel"))
            if len(model_candidates) != 1:
                raise SemanticProjectError("Managed PBIP must contain exactly one semantic model")
            self._write_semantic_model(
                model_candidates[0],
                relinked_manifest,
                dashboard,
            )
            (stage / ".ai-powerbi" / "source-manifest.json").write_text(
                relinked_manifest.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            receipt = self.validate(stage, relinked_manifest, dashboard)
            if not receipt.passed:
                raise SemanticProjectError("Relinked PBIP failed validation")
            self.backup_root.mkdir(parents=True, exist_ok=True)
            os.replace(project, backup)
            try:
                os.replace(stage, project)
            except Exception:
                os.replace(backup, project)
                raise
            promoted = receipt.model_copy(update={"project_hash": hash_tree(project)})
            self.evidence_root.mkdir(parents=True, exist_ok=True)
            (self.evidence_root / f"{project.name}-validation-receipt.json").write_text(
                promoted.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            self.undo_root.mkdir(parents=True, exist_ok=True)
            (self.undo_root / f"{project.name}.json").write_text(
                json.dumps({"backup_path": str(backup)}, indent=2) + "\n",
                encoding="utf-8",
            )
            return promoted
        except Exception:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    def validate(
        self,
        project: Path,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
    ) -> ValidationReceipt:
        issues: list[str] = []
        pbip_candidates = list(project.glob("*.pbip"))
        if len(pbip_candidates) != 1:
            issues.append("Project must contain exactly one PBIP entrypoint")
        for source in manifest.sources:
            snapshot = Path(source.snapshot_path)
            if not snapshot.is_file() or sha256_file(snapshot) != source.sha256:
                issues.append(f"Source snapshot hash changed: {source.original_name}")
        for json_path in project.rglob("*.json"):
            try:
                json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                issues.append(f"Invalid JSON: {json_path.relative_to(project)}")
        table_ids = {table.id for table in manifest.tables}
        columns = {(table.id, column.id) for table in manifest.tables for column in table.columns}
        measures = {measure.id for measure in dashboard.semantic_model.measures}
        for page in dashboard.pages:
            for visual in page.visuals:
                if visual.x + visual.width > 1280 or visual.y + visual.height > 720:
                    issues.append(f"Visual is outside the page canvas: {visual.id}")
                for binding in visual.bindings:
                    if binding.table_id not in table_ids:
                        issues.append(f"Unknown visual table: {binding.table_id}")
                    elif (
                        binding.field_kind == "column"
                        and (
                            binding.table_id,
                            binding.field_id,
                        )
                        not in columns
                    ):
                        issues.append(f"Unknown visual column: {binding.field_id}")
                    elif binding.field_kind == "measure" and binding.field_id not in measures:
                        issues.append(f"Unknown visual measure: {binding.field_id}")
        try:
            table_plans = _semantic_table_plan_index(manifest, dashboard)
        except SemanticProjectError as error:
            issues.append(str(error))
            table_plans = {
                table.id: SemanticTablePlan(table_id=table.id, display_name=table.name)
                for table in manifest.tables
            }
        semantic_table_names = {
            table_id: plan.display_name for table_id, plan in table_plans.items()
        }
        calendar = (
            plan_calendar_table(
                manifest,
                preferred_name=_calendar_display_name(semantic_table_names.values()),
            )
            if dashboard.semantic_model.create_calendar
            else None
        )
        issues.extend(
            _tmdl_model_index_issues(
                project,
                manifest,
                semantic_table_names=semantic_table_names,
                calendar_table_name=calendar.table_name if calendar else None,
            )
        )
        validators = [
            "local-pbip-structure",
            "source-content-hash",
            "semantic-bindings",
            "local-tmdl-model-index",
        ]
        if not issues:
            external_issues, external_validators = self._external_validation(project)
            issues.extend(external_issues)
            validators.extend(external_validators)
        return ValidationReceipt(
            id=f"validation-{uuid.uuid4().hex}",
            created_at=datetime.now(UTC),
            passed=not issues,
            project_hash=hash_tree(project) if not issues else None,
            validators=validators,
            issues=issues,
        )

    def _external_validation(self, project: Path) -> tuple[list[str], list[str]]:
        issues: list[str] = []
        validators: list[str] = []
        pbip = next(project.glob("*.pbip"), None)
        report = next(project.glob("*.Report"), None)
        if pbip is None or report is None:
            return ["PBIP or report directory is missing"], validators
        pbi = probe_pbi_cli()
        author = probe_tool("powerbi-report-author")
        if not pbi.available or pbi.path is None:
            if self.require_external_validators:
                issues.append("Pinned pbi-cli validator is unavailable")
        else:
            validators.append("pbi-cli")
            issues.extend(
                _validator_issues(
                    (
                        pbi.path,
                        "--json",
                        "report",
                        "--path",
                        str(report),
                        "validate",
                        "--full",
                    ),
                    "pbi-cli",
                )
            )
        if not author.available or author.path is None:
            if self.require_external_validators:
                issues.append("Microsoft PBIR validator is unavailable")
        else:
            validators.append("microsoft-pbir-validator")
            issues.extend(
                _validator_issues(
                    (author.path, "validate", str(pbip)),
                    "Microsoft PBIR validator",
                )
            )
        return issues, validators

    def _write_project(
        self,
        project: Path,
        name: str,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
        *,
        project_key: str | None = None,
    ) -> None:
        project.mkdir(parents=True, exist_ok=False)
        project_id = _identifier(project_key or name)
        report_name = f"{project_id}.Report"
        model_name = f"{project_id}.SemanticModel"
        report = project / report_name
        model = project / model_name
        (report / "definition" / "pages").mkdir(parents=True)
        (model / "definition" / "tables").mkdir(parents=True)
        _write_json(
            project / f"{project_id}.pbip",
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/pbip/pbipProperties/1.0.0/schema.json",
                "version": "1.0",
                "artifacts": [{"report": {"path": report_name}}],
                "settings": {"enableAutoRecovery": True},
            },
        )
        _write_json(
            report / ".platform",
            _platform_properties(name, project_id, artifact_type="Report"),
        )
        _write_json(
            model / ".platform",
            _platform_properties(name, project_id, artifact_type="SemanticModel"),
        )
        _write_json(
            report / "definition.pbir",
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definitionProperties/2.0.0/schema.json",
                "version": "4.0",
                "datasetReference": {"byPath": {"path": f"../{model_name}"}},
            },
        )
        shutil.copy2(
            self.template_root / "ClientWow.Report" / "definition" / "report.json",
            report / "definition" / "report.json",
        )
        register_studio_theme(report, dashboard.report_theme)
        shutil.copy2(
            self.template_root / "ClientWow.Report" / "definition" / "version.json",
            report / "definition" / "version.json",
        )
        shutil.copy2(
            self.template_root / "ClientWow.SemanticModel" / "definition.pbism",
            model / "definition.pbism",
        )
        self._write_semantic_model(model, manifest, dashboard)
        self._write_report(report, manifest, dashboard)
        evidence = project / ".ai-powerbi"
        evidence.mkdir()
        (evidence / "source-manifest.json").write_text(
            manifest.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        (evidence / "dashboard-plan.json").write_text(
            dashboard.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    def _write_semantic_model(
        self,
        model: Path,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
    ) -> None:
        definition = model / "definition"
        table_plans = _semantic_table_plan_index(manifest, dashboard)
        semantic_table_names = {
            table_id: plan.display_name for table_id, plan in table_plans.items()
        }
        source_to_semantic_names = {
            table.name: semantic_table_names[table.id] for table in manifest.tables
        }
        calendar = (
            plan_calendar_table(
                manifest,
                preferred_name=_calendar_display_name(semantic_table_names.values()),
            )
            if dashboard.semantic_model.create_calendar
            else None
        )
        (definition / "database.tmdl").write_text(
            "database SemanticModel\n\tcompatibilityLevel: 1604\n",
            encoding="utf-8",
        )
        reference_lines = [
            f"ref table {_quote_name(semantic_table_names[table.id])}" for table in manifest.tables
        ]
        if calendar is not None:
            reference_lines.append(calendar.model_reference_tmdl)
        refs = "\n".join(reference_lines)
        (definition / "model.tmdl").write_text(
            "model Model\n\tculture: en-US\n\tdefaultPowerBIDataSourceVersion: powerBI_V3\n"
            "\tdiscourageImplicitMeasures\n\n" + refs + "\n",
            encoding="utf-8",
        )
        source_by_id = {source.id: source for source in manifest.sources}
        measures_by_table: dict[str, list[DslMeasureSpec | ProposedDaxMeasure]] = {}
        measure_names = {measure.id: measure.name for measure in dashboard.semantic_model.measures}
        for measure in dashboard.semantic_model.measures:
            table_id = (
                measure.table_id
                if isinstance(measure, DslMeasureSpec) or measure.table_id is not None
                else manifest.tables[0].id
            )
            if table_id not in {table.id for table in manifest.tables}:
                raise SemanticProjectError(
                    f"Measure {measure.id} targets an unknown semantic table"
                )
            measures_by_table.setdefault(table_id, []).append(measure)
        date_table = None if calendar is not None else _related_date_table(manifest, dashboard)
        date_refs: dict[str, dict[str, str]] = {}
        if date_table is not None:
            date_ref = (
                f"{_quote_name(semantic_table_names[date_table[0].id])}[{date_table[1].name}]"
            )
            for item in dashboard.semantic_model.relationships:
                if (
                    item.is_active
                    and not item.blockers
                    and (item.to_table_id, item.to_column_id)
                    == (date_table[0].id, date_table[1].id)
                ):
                    date_refs.setdefault(item.from_table_id, {})[item.from_column_id] = date_ref
        for table in manifest.tables:
            source = source_by_id[table.source_id]
            table_plan = table_plans[table.id]
            text = _table_tmdl(
                table,
                source,
                measures_by_table.get(table.id, []),
                measure_names,
                semantic_table_name=table_plan.display_name,
                semantic_table_names=source_to_semantic_names,
                hidden_column_ids=table_plan.hidden_column_ids,
                date_key_column_id=(
                    date_table[1].id
                    if date_table is not None and date_table[0].id == table.id
                    else None
                ),
                date_table_refs=date_refs.get(table.id),
            )
            (definition / "tables" / f"{_safe_part(table.name)}.tmdl").write_text(
                text,
                encoding="utf-8",
            )
        if calendar is not None:
            (definition / "tables" / f"{_safe_part(calendar.table_name)}.tmdl").write_text(
                calendar.tmdl, encoding="utf-8"
            )
        relationships = []
        table_by_id = {table.id: table for table in manifest.tables}
        for relationship in dashboard.semantic_model.relationships:
            left = table_by_id[relationship.from_table_id]
            right = table_by_id[relationship.to_table_id]
            left_column = next(
                item for item in left.columns if item.id == relationship.from_column_id
            )
            right_column = next(
                item for item in right.columns if item.id == relationship.to_column_id
            )
            properties: list[str] = []
            if relationship.cardinality == "one_to_one":
                if relationship.cross_filter_direction != "both":
                    raise SemanticProjectError(
                        "Power BI one-to-one relationships require Both cross-filter direction"
                    )
                properties.extend(["\tfromCardinality: one\n", "\ttoCardinality: one\n"])
            if relationship.cross_filter_direction == "both":
                properties.append("\tcrossFilteringBehavior: bothDirections\n")
            if not relationship.is_active:
                properties.append("\tisActive: false\n")
            if relationship.rely_on_referential_integrity:
                properties.append("\trelyOnReferentialIntegrity: true\n")
            relationships.append(
                f"relationship {uuid.uuid5(uuid.NAMESPACE_URL, relationship.id)}\n"
                f"\tfromColumn: {_quote_name(semantic_table_names[left.id])}."
                f"{_quote_name(left_column.name)}\n"
                f"\ttoColumn: {_quote_name(semantic_table_names[right.id])}."
                f"{_quote_name(right_column.name)}\n" + "".join(properties)
            )
        if calendar is not None:
            for relationship_spec in calendar.relationship_specs:
                relationship = relationship_spec.relationship
                left = table_by_id[relationship.from_table_id]
                left_column = next(
                    item for item in left.columns if item.id == relationship.from_column_id
                )
                relationships.append(
                    f"relationship {uuid.uuid5(uuid.NAMESPACE_URL, relationship.id)}\n"
                    f"\tfromColumn: {_quote_name(semantic_table_names[left.id])}."
                    f"{_quote_name(left_column.name)}\n"
                    f"\ttoColumn: {_quote_name(calendar.table_name)}.Date\n"
                    f"\t{relationship_spec.tmdl_property}\n"
                )
        (definition / "relationships.tmdl").write_text(
            "\n".join(relationships),
            encoding="utf-8",
        )

    def _write_report(
        self,
        report: Path,
        manifest: SourceManifest,
        dashboard: DashboardPlan,
    ) -> None:
        pages_root = report / "definition" / "pages"
        page_names: list[str] = []
        tables = {table.id: table for table in manifest.tables}
        table_plans = _semantic_table_plan_index(manifest, dashboard)
        semantic_table_names = {
            table_id: plan.display_name for table_id, plan in table_plans.items()
        }
        measures = {measure.id: measure for measure in dashboard.semantic_model.measures}
        month_axes = _calendar_month_axes(manifest, dashboard, semantic_table_names)
        for page in dashboard.pages:
            page_name = uuid.uuid5(uuid.NAMESPACE_URL, page.id).hex[:20]
            page_names.append(page_name)
            page_root = pages_root / page_name
            visuals_root = page_root / "visuals"
            visuals_root.mkdir(parents=True)
            _write_json(
                page_root / "page.json",
                {
                    "$schema": _PAGE_SCHEMA,
                    "name": page_name,
                    "displayName": page.title,
                    "displayOption": "FitToPage",
                    "height": 720,
                    "width": 1280,
                },
            )
            for visual_index, visual in enumerate(page.visuals):
                visual_name = uuid.uuid5(uuid.NAMESPACE_URL, f"{page.id}:{visual.id}").hex[:20]
                visual_root = visuals_root / visual_name
                visual_root.mkdir()
                query_state: dict[str, object] = {}
                for binding in visual.bindings:
                    calendar_name = (
                        month_axes.get((binding.table_id, binding.field_id))
                        if binding.role == "Category" and binding.field_kind == "column"
                        else None
                    )
                    projection = (
                        _month_axis_projection(calendar_name)
                        if calendar_name
                        else _projection(
                            binding,
                            tables,
                            measures,
                            semantic_table_names,
                        )
                    )
                    role = query_state.setdefault(binding.role, {"projections": []})
                    assert isinstance(role, dict)
                    projections = role["projections"]
                    assert isinstance(projections, list)
                    projections.append(projection)
                query: dict[str, object] = {"queryState": query_state}
                ranking = _ranking_sort(visual, tables, measures, semantic_table_names)
                if ranking is not None:
                    query["sortDefinition"] = ranking
                visual_objects = (_card_label_objects(visual, query_state) or {}) | (
                    visual_forecast_objects(visual, query_state, tables) or {}
                )
                _write_json(
                    visual_root / "visual.json",
                    {
                        **(
                            {
                                "filterConfig": date_filter_config(
                                    visual.date_filter,
                                    visual_id=visual_name,
                                    table=tables[visual.date_filter.table_id],
                                    semantic_name=semantic_table_names[visual.date_filter.table_id],
                                )
                            }
                            if visual.date_filter
                            else {}
                        ),
                        "$schema": _VISUAL_SCHEMA,
                        "name": visual_name,
                        "position": {
                            "x": visual.x,
                            "y": visual.y,
                            "z": visual_index,
                            "height": visual.height,
                            "width": visual.width,
                            "tabOrder": visual_index,
                        },
                        "visual": {
                            "visualType": visual.visual_type,
                            **({"objects": visual_objects} if visual_objects else {}),
                            "visualContainerObjects": _container_objects(visual.title),
                            "query": query,
                            "drillFilterOtherVisuals": True,
                        },
                    },
                )
        _write_json(
            pages_root / "pages.json",
            {
                "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/report/definition/pagesMetadata/1.0.0/schema.json",
                "pageOrder": page_names,
                "activePageName": page_names[0],
            },
        )


def compile_dsl_measure(
    spec: DslMeasureSpec,
    table: TableProfile,
    measure_names: dict[str, str] | None = None,
    *,
    table_name: str | None = None,
    date_table_refs: Mapping[str, str] | None = None,
) -> str:
    """DAX for a governed measure.

    ``date_table_refs`` maps a fact date column to its marked date table's column
    ('Dates'[Date]). Time intelligence must use that column: with DATEADD on
    Sales[OrderDate], a report sliced by Dates[Year] keeps the Dates filter and
    the previous period is always blank.
    """
    semantic_table_name = table_name or table.name
    column = next((item for item in table.columns if item.id == spec.column_id), None)
    column_ref = (
        f"{_quote_name(semantic_table_name)}[{column.name}]" if column is not None else None
    )
    weight_column = next(
        (item for item in table.columns if item.id == spec.weight_column_id),
        None,
    )
    weight_ref = (
        f"{_quote_name(semantic_table_name)}[{weight_column.name}]"
        if weight_column is not None
        else None
    )
    date_column = next(
        (item for item in table.columns if item.id == spec.date_column_id),
        None,
    )
    date_ref = (
        (date_table_refs or {}).get(date_column.id)
        or f"{_quote_name(semantic_table_name)}[{date_column.name}]"
        if date_column is not None
        else None
    )
    operations = {
        "sum": lambda: f"SUM({column_ref})",
        "count": lambda: (
            f"COUNTROWS({_quote_name(semantic_table_name)})"
            if column_ref is None
            else f"COUNTA({column_ref})"
        ),
        "distinct_count": lambda: f"DISTINCTCOUNT({column_ref})",
        "average": lambda: f"AVERAGE({column_ref})",
        "minimum": lambda: f"MIN({column_ref})",
        "maximum": lambda: f"MAX({column_ref})",
    }
    if spec.operation in operations:
        if spec.operation != "count" and column_ref is None:
            raise SemanticProjectError(f"Measure {spec.id} requires a column")
        return operations[spec.operation]()
    if spec.operation in {"ratio", "margin"}:
        if not spec.numerator_measure_id or not spec.denominator_measure_id:
            raise SemanticProjectError(f"Measure {spec.id} requires numerator and denominator")
        names = measure_names or {}
        numerator = names.get(spec.numerator_measure_id, spec.numerator_measure_id)
        denominator = names.get(spec.denominator_measure_id, spec.denominator_measure_id)
        return f"DIVIDE([{numerator}], [{denominator}])"
    names = measure_names or {}
    if spec.operation == "weighted_average":
        if column is None or column_ref is None or weight_column is None or weight_ref is None:
            raise SemanticProjectError(f"Measure {spec.id} requires value and weight columns")
        if column.data_type.value not in {"integer", "number"} or (
            weight_column is not None and weight_column.data_type.value not in {"integer", "number"}
        ):
            raise SemanticProjectError(
                f"Measure {spec.id} requires numeric value and weight columns"
            )
        table_ref = _quote_name(semantic_table_name)
        return f"DIVIDE(SUMX({table_ref}, {column_ref} * {weight_ref}), SUM({weight_ref}))"
    if spec.operation in {"running_total", "period_to_date", "period_change"}:
        if not spec.base_measure_id or date_column is None or date_ref is None:
            raise SemanticProjectError(f"Measure {spec.id} requires a base measure and date column")
        if date_column.data_type.value not in {"date", "datetime"}:
            raise SemanticProjectError(f"Measure {spec.id} requires a date column")
        base = names.get(spec.base_measure_id, spec.base_measure_id)
        if spec.operation == "running_total":
            return (
                f"CALCULATE([{base}], FILTER(ALLSELECTED({date_ref}), "
                f"{date_ref} <= MAX({date_ref})))"
            )
        if spec.operation == "period_to_date":
            function = {"month": "TOTALMTD", "quarter": "TOTALQTD", "year": "TOTALYTD"}[spec.period]
            return f"{function}([{base}], {date_ref})"
        interval = spec.period.upper()
        return (
            f"VAR PreviousValue = CALCULATE([{base}], DATEADD({date_ref}, -1, {interval})) "
            f"RETURN DIVIDE([{base}] - PreviousValue, PreviousValue)"
        )
    if spec.operation == "variance":
        if not spec.numerator_measure_id or not spec.denominator_measure_id:
            raise SemanticProjectError(f"Measure {spec.id} requires actual and comparison measures")
        actual = names.get(spec.numerator_measure_id, spec.numerator_measure_id)
        comparison = names.get(spec.denominator_measure_id, spec.denominator_measure_id)
        return f"[{actual}] - [{comparison}]"
    raise SemanticProjectError(f"Unsupported governed measure operation: {spec.operation}")


def validate_advanced_dax(
    spec: ProposedDaxMeasure,
    manifest: SourceManifest,
    measure_ids: set[str],
    measure_names: dict[str, str] | None = None,
) -> None:
    if not spec.approved:
        raise SemanticProjectError(f"Advanced DAX measure {spec.id} needs extra approval")
    if spec.table_id is not None and spec.table_id not in {table.id for table in manifest.tables}:
        raise SemanticProjectError(
            f"Advanced DAX measure {spec.id} targets an unknown semantic table"
        )
    if re.search(
        r"\b(?:WEBSERVICE|EXTERNALTOOLS|OPENROWSET|EVALUATEANDLOG)\b",
        spec.expression,
        re.I,
    ):
        raise SemanticProjectError(f"Advanced DAX measure {spec.id} uses a blocked function")
    unsupported_functions = unsupported_dax_functions(spec.expression)
    if unsupported_functions:
        raise SemanticProjectError(
            f"Advanced DAX measure {spec.id} uses unsupported DAX function(s): "
            + ", ".join(sorted(unsupported_functions))
        )
    argument_errors = dax_argument_errors(spec.expression)
    if argument_errors:
        raise SemanticProjectError("; ".join(argument_errors))
    table_names = {table.name for table in manifest.tables}
    column_names = {
        (table.name, column.name) for table in manifest.tables for column in table.columns
    }
    qualified_spans: list[tuple[int, int]] = []
    qualified_pattern = re.compile(
        r"(?:'(?P<quoted>[^']+)'|(?P<plain>[A-Za-z_][A-Za-z0-9_ ]*))"
        r"\[(?P<column>[^\]]+)\]"
    )
    for match in qualified_pattern.finditer(spec.expression):
        table_name = match.group("quoted") or match.group("plain")
        column_name = match.group("column")
        if table_name not in table_names or (table_name, column_name) not in column_names:
            raise SemanticProjectError(f"Advanced DAX measure {spec.id} has an unknown column")
        qualified_spans.append(match.span())
    for dependency in spec.dependencies:
        if dependency not in measure_ids:
            raise SemanticProjectError(f"Advanced DAX measure {spec.id} has an unknown dependency")
    expression_without_columns = spec.expression
    for start, end in reversed(qualified_spans):
        expression_without_columns = (
            expression_without_columns[:start] + expression_without_columns[end:]
        )
    known_measure_names = set((measure_names or {}).values())
    referenced_measure_names = set(re.findall(r"\[([^\]]+)\]", expression_without_columns))
    if not referenced_measure_names.issubset(known_measure_names):
        raise SemanticProjectError(f"Advanced DAX measure {spec.id} has an unknown measure")


def _validate_measure_graph(
    manifest: SourceManifest,
    measures: list[DslMeasureSpec | ProposedDaxMeasure],
) -> None:
    measure_ids = {measure.id for measure in measures}
    measure_names = {measure.id: measure.name for measure in measures}
    dependencies: dict[str, set[str]] = {}
    for measure in measures:
        # Names and scalar properties must never introduce new TMDL declarations,
        # including when an internal model_copy bypassed Pydantic construction.
        _quote_name(measure.name)
        if not measure.format_string or not measure.format_string.isprintable():
            raise SemanticProjectError("Measure format must be a nonempty single-line value")
        if isinstance(measure, ProposedDaxMeasure):
            validate_advanced_dax(measure, manifest, measure_ids, measure_names)
            dependencies[measure.id] = set(measure.dependencies)
        elif measure.operation in {"ratio", "margin", "variance"}:
            required = {measure.numerator_measure_id, measure.denominator_measure_id}
            if None in required or not required.issubset(measure_ids):
                raise SemanticProjectError(f"Measure {measure.id} has an unknown dependency")
            dependencies[measure.id] = {value for value in required if value is not None}
        elif measure.operation in {"running_total", "period_to_date", "period_change"}:
            base_dependency = measure.base_measure_id
            if base_dependency is None or base_dependency not in measure_ids:
                raise SemanticProjectError(f"Measure {measure.id} has an unknown dependency")
            dependencies[measure.id] = {base_dependency}
        else:
            dependencies[measure.id] = set()

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(measure_id: str) -> None:
        if measure_id in visiting:
            raise SemanticProjectError("Measure dependencies contain a circular reference")
        if measure_id in visited:
            return
        visiting.add(measure_id)
        for dependency in dependencies[measure_id]:
            visit(dependency)
        visiting.remove(measure_id)
        visited.add(measure_id)

    for measure_id in dependencies:
        visit(measure_id)


def _semantic_table_plan_index(
    manifest: SourceManifest,
    dashboard: DashboardPlan,
) -> dict[str, SemanticTablePlan]:
    """Validate and index the reviewed semantic table presentation contract."""

    plans = dashboard.semantic_model.tables
    plans_by_id = {plan.table_id: plan for plan in plans}
    if len(plans_by_id) != len(plans):
        raise SemanticProjectError("Semantic table plan contains duplicate table IDs")
    manifest_ids = {table.id for table in manifest.tables}
    plan_ids = set(plans_by_id)
    if plan_ids != manifest_ids:
        missing = sorted(manifest_ids - plan_ids)
        unknown = sorted(plan_ids - manifest_ids)
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise SemanticProjectError(
            "Semantic table plan does not match the reviewed manifest: " + "; ".join(details)
        )

    normalized_names: dict[str, str] = {}
    for table in manifest.tables:
        plan = plans_by_id[table.id]
        if not plan.display_name.strip():
            raise SemanticProjectError(f"Semantic table {table.id} has a blank display name")
        if not plan.display_name.isprintable():
            raise SemanticProjectError(
                f"Semantic table {table.id} display name contains a control character"
            )
        normalized_name = plan.display_name.casefold()
        previous = normalized_names.get(normalized_name)
        if previous is not None:
            raise SemanticProjectError(
                f"Semantic table display names must be unique: {previous} and {plan.display_name}"
            )
        normalized_names[normalized_name] = plan.display_name
        hidden_ids = set(plan.hidden_column_ids)
        if len(hidden_ids) != len(plan.hidden_column_ids):
            raise SemanticProjectError(f"Semantic table {table.id} repeats a hidden column ID")
        unknown_hidden = hidden_ids - {column.id for column in table.columns}
        if unknown_hidden:
            raise SemanticProjectError(
                f"Semantic table {table.id} hides unknown column(s): "
                + ", ".join(sorted(unknown_hidden))
            )
    return plans_by_id


def _calendar_display_name(semantic_table_names: Collection[str]) -> str:
    existing = {name.casefold() for name in semantic_table_names}
    suffix = 1
    while True:
        candidate = "Calendar" if suffix == 1 else f"Calendar {suffix}"
        if candidate.casefold() not in existing:
            return candidate
        suffix += 1


def _tmdl_model_index_issues(
    project: Path,
    manifest: SourceManifest,
    *,
    semantic_table_names: Mapping[str, str] | None = None,
    calendar_table_name: str | None = None,
) -> list[str]:
    definitions = list(project.glob("*.SemanticModel/definition"))
    if len(definitions) != 1:
        return ["Project must contain exactly one semantic model definition"]
    definition = definitions[0]
    model_path = definition / "model.tmdl"
    try:
        lines = model_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        return [f"TMDL model index could not be read: {error}"]

    issues: list[str] = []
    reference_pattern = re.compile(r"^ref table (?:'((?:''|[^'])+)'|([^\s]+))$")
    references: list[str] = []
    for line_number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("ref ") and stripped != line:
            issues.append(
                "TMDL object references must be root-level: "
                f"{model_path.relative_to(project)}:{line_number}"
            )
            continue
        match = reference_pattern.fullmatch(line)
        if match:
            references.append((match.group(1) or match.group(2)).replace("''", "'"))

    names = semantic_table_names or {}
    expected = [names.get(table.id, table.name) for table in manifest.tables]
    if calendar_table_name is not None:
        expected.append(calendar_table_name)
    if len(references) != len(set(references)):
        issues.append("TMDL model index contains duplicate table references")
    if set(references) != set(expected):
        missing = sorted(set(expected) - set(references))
        unexpected = sorted(set(references) - set(expected))
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        issues.append(
            "TMDL model index table references do not match the manifest: " + "; ".join(details)
        )
    return issues


def _identifier_number(column: ColumnProfile) -> bool:
    """Numbers that name things (Postal Code, Row ID, StoreKey) must never sum."""
    return column.geographic_role == "postal_code" or bool(
        re.search(r"(?:^|[\s_-])(?:id|key|code)$", column.name, re.IGNORECASE)
        or re.search(r"(?<=[a-z])(?:ID|Id|Key|Code)$", column.name)
    )


def _table_tmdl(
    table: TableProfile,
    source: SourceFile,
    measures: list[DslMeasureSpec | ProposedDaxMeasure],
    measure_names: dict[str, str] | None = None,
    *,
    semantic_table_name: str | None = None,
    semantic_table_names: Mapping[str, str] | None = None,
    hidden_column_ids: Collection[str] = (),
    date_key_column_id: str | None = None,
    date_table_refs: Mapping[str, str] | None = None,
) -> str:
    table_name = semantic_table_name or table.name
    hidden_ids = set(hidden_column_ids)
    lines = [f"table {_quote_name(table_name)}"]
    if date_key_column_id is not None:
        # Mark as date table: time intelligence and date filters use this table.
        lines.extend(["\tdataCategory: Time", ""])
    for column in table.columns:
        lines.extend(
            [
                f"\tcolumn {_quote_name(column.name)}",
                f"\t\tdataType: {_tmdl_type(column.data_type.value)}",
            ]
        )
        if column.id == date_key_column_id:
            lines.append("\t\tisKey")
        if column.id in hidden_ids:
            lines.append("\t\tisHidden")
        if column.data_type.value not in {"integer", "number"} or _identifier_number(column):
            lines.append("\t\tsummarizeBy: none")
        lines.extend([f"\t\tsourceColumn: {column.name}", ""])
    date_key = next((c for c in table.columns if c.id == date_key_column_id), None)
    if date_key is not None and _month_start_available(table):
        # Trend axes bind here so Desktop plots months, like the Studio preview.
        reference = f"{_quote_name(table_name)}[{date_key.name}]"
        lines.extend(
            [
                f"\tcolumn {_quote_name(MONTH_START_COLUMN)} = "
                f"DATE(YEAR({reference}), MONTH({reference}), 1)",
                "\t\tdataType: dateTime",
                "\t\tformatString: mmm yyyy",
                "\t\tsummarizeBy: none",
                "",
            ]
        )
    for measure in measures:
        if isinstance(measure, DslMeasureSpec):
            expression = compile_dsl_measure(
                measure,
                table,
                measure_names,
                table_name=table_name,
                date_table_refs=date_table_refs,
            )
        else:
            expression = _remap_dax_table_names(
                measure.expression,
                semantic_table_names or {},
            )
        lines.extend(
            [
                f"\tmeasure {_quote_name(measure.name)} = {expression}",
                "\t\tformatString: " + measure.format_string,
                "",
            ]
        )
    lines.extend(
        [
            f"\tpartition {_quote_name(table_name)} = m",
            "\t\tmode: import",
            "\t\tsource =",
            *[f"\t\t\t{line}" for line in _power_query(table, source).splitlines()],
            "",
        ]
    )
    return "\n".join(lines)


def _related_date_table(
    manifest: SourceManifest, dashboard: DashboardPlan
) -> tuple[TableProfile, ColumnProfile] | None:
    """The date dimension an approved, active relationship points at (Dates[Date])."""
    tables = {table.id: table for table in manifest.tables}
    for item in dashboard.semantic_model.relationships:
        table = tables.get(item.to_table_id)
        if table is None or not item.is_active or item.blockers:
            continue
        key = daily_date_key(table)
        if key is not None and key.id == item.to_column_id:
            return table, key
    return None


def _remap_dax_table_names(
    expression: str,
    semantic_table_names: Mapping[str, str],
) -> str:
    reference_targets: dict[str, str] = {}
    reference_patterns: list[str] = []
    for source_name, target_name in semantic_table_names.items():
        if target_name == source_name:
            continue
        quoted_source = "'" + source_name.replace("'", "''") + "'"
        reference_targets[quoted_source.casefold()] = target_name
        reference_patterns.append(re.escape(quoted_source))
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source_name):
            reference_targets[source_name.casefold()] = target_name
            reference_patterns.append(re.escape(source_name) + r"(?=\[)")
    if not reference_patterns:
        return expression

    pattern = re.compile(
        r"(?<![A-Za-z0-9_'])(?:"
        + "|".join(sorted(reference_patterns, key=len, reverse=True))
        + r")",
        re.IGNORECASE,
    )
    string_segments = re.split(r'("(?:[^"]|"")*")', expression)
    string_segments = [
        segment
        if index % 2
        else pattern.sub(
            lambda match: _quote_name(reference_targets[match.group(0).casefold()]),
            segment,
        )
        for index, segment in enumerate(string_segments)
    ]
    return "".join(string_segments)


def _power_query(table: TableProfile, source: SourceFile) -> str:
    path = _m_string(source.snapshot_path)
    type_pairs = ", ".join(
        "{" + _m_string(column.name) + ", " + _m_type(column.data_type.value) + "}"
        for column in table.columns
    )
    if source.kind is SourceKind.CSV:
        encoding = 65001 if (source.encoding or "").startswith("utf-8") else 1252
        delimiter = _m_string(source.delimiter or ",")
        source_expression = (
            f"Csv.Document(File.Contents({path}), [Delimiter={delimiter}, Encoding={encoding}, "
            "QuoteStyle=QuoteStyle.Csv])"
        )
        select = "Source"
    elif source.kind is SourceKind.EXCEL:
        source_expression = f"Excel.Workbook(File.Contents({path}), null, true)"
        sheet = _m_string(table.sheet or table.name)
        select = f'Source{{[Item={sheet}, Kind="Sheet"]}}[Data]'
    else:
        source_expression = f"Parquet.Document(File.Contents({path}))"
        select = "Source"
        return (
            "let\n"
            f"    Source = {source_expression},\n"
            f"    ChangedType = Table.TransformColumnTypes({select}, {{{type_pairs}}})\n"
            "in\n"
            "    ChangedType"
        )
    return (
        "let\n"
        f"    Source = {source_expression},\n"
        f"    Selected = {select},\n"
        "    PromotedHeaders = Table.PromoteHeaders(Selected, [PromoteAllScalars=true]),\n"
        f"    ChangedType = Table.TransformColumnTypes(PromotedHeaders, {{{type_pairs}}})\n"
        "in\n"
        "    ChangedType"
    )


def _projection(
    binding: VisualRoleBinding,
    tables: dict[str, TableProfile],
    measures: dict[str, DslMeasureSpec | ProposedDaxMeasure],
    semantic_table_names: Mapping[str, str] | None = None,
) -> dict[str, object]:
    table = tables[binding.table_id]
    table_name = (semantic_table_names or {}).get(binding.table_id, table.name)
    if binding.field_kind == "column":
        member = next(item.name for item in table.columns if item.id == binding.field_id)
        semantic_kind = "Column"
    else:
        member = measures[binding.field_id].name
        semantic_kind = "Measure"
    query_ref = f"{table_name}.{member}"
    return {
        "field": {
            semantic_kind: {
                "Expression": {"SourceRef": {"Entity": table_name}},
                "Property": member,
            }
        },
        "queryRef": query_ref,
        "nativeQueryRef": member,
    }


def _month_start_available(table: TableProfile) -> bool:
    """A source column named Month Start would collide with the calculated one."""
    return all(column.name.casefold() != MONTH_START_COLUMN.casefold() for column in table.columns)


def _calendar_month_axes(
    manifest: SourceManifest,
    dashboard: DashboardPlan,
    semantic_table_names: Mapping[str, str],
) -> dict[tuple[str, str], str]:
    """Date columns whose trend axis becomes a Month Start column, mapped to its table.

    The generated calendar joins fact dates; a related date dimension (Dates[Date])
    gets a calculated Month Start instead, for its key and the fact dates it filters.
    """
    if not dashboard.semantic_model.create_calendar:
        related = _related_date_table(manifest, dashboard)
        if related is None or not _month_start_available(related[0]):
            return {}
        table, key = related
        name = semantic_table_names.get(table.id, table.name)
        axes = {(table.id, key.id): name}
        for item in dashboard.semantic_model.relationships:
            if (
                item.is_active
                and not item.blockers
                and (item.to_table_id, item.to_column_id) == (table.id, key.id)
            ):
                axes[(item.from_table_id, item.from_column_id)] = name
        return axes
    calendar = plan_calendar_table(
        manifest,
        preferred_name=_calendar_display_name(semantic_table_names.values()),
    )
    if calendar is None:
        return {}
    return {
        (spec.relationship.from_table_id, spec.relationship.from_column_id): calendar.table_name
        for spec in calendar.relationship_specs
        if spec.relationship.is_active
    }


def _card_label_objects(
    visual: VisualPlan, query_state: dict[str, object]
) -> dict[str, object] | None:
    """Hide a card's measure label when the card title already names the measure.

    Desktop drew "Total Sales" twice: once as the title, once under the value.
    """
    if visual.visual_type != "cardVisual":
        return None
    data = query_state.get("Data")
    projections = data.get("projections", []) if isinstance(data, dict) else []
    if len(projections) != 1:
        return None
    projection = projections[0]
    if projection.get("nativeQueryRef", "").casefold() != visual.title.strip().casefold():
        return None
    return {
        "label": [
            {
                "properties": {"show": {"expr": {"Literal": {"Value": "false"}}}},
                "selector": {"metadata": projection["queryRef"]},
            }
        ]
    }


def _month_axis_projection(calendar_name: str) -> dict[str, object]:
    return {
        "field": {
            "Column": {
                "Expression": {"SourceRef": {"Entity": calendar_name}},
                "Property": MONTH_START_COLUMN,
            }
        },
        "queryRef": f"{calendar_name}.{MONTH_START_COLUMN}",
        "nativeQueryRef": MONTH_START_COLUMN,
    }


def _ranking_sort(
    visual: VisualPlan,
    tables: dict[str, TableProfile],
    measures: dict[str, DslMeasureSpec | ProposedDaxMeasure],
    semantic_table_names: Mapping[str, str],
) -> dict[str, object] | None:
    """Rank a category chart by its measure, as the Studio preview does.

    Without a sort Desktop ordered Total Profit by Region alphabetically. Time axes,
    tables and matrices keep their natural order.
    """
    if re.search(r"line|area|table|matrix", visual.visual_type, re.IGNORECASE):
        return None
    categories = [item for item in visual.bindings if item.role == "Category"]
    values = [item for item in visual.bindings if item.field_kind == "measure"]
    if len(categories) != 1 or len(values) != 1 or categories[0].field_kind != "column":
        return None
    column = next(
        (
            item
            for item in tables[categories[0].table_id].columns
            if item.id == categories[0].field_id
        ),
        None,
    )
    if column is None or column.data_type.value in {"date", "datetime"}:
        return None
    field = _projection(values[0], tables, measures, semantic_table_names)["field"]
    return {"sort": [{"field": field, "direction": "Descending"}], "isDefaultSort": True}


def _container_objects(title: str) -> dict[str, object]:
    literal = {"expr": {"Literal": {"Value": "true"}}}
    return {
        "border": [{"properties": {"show": literal}}],
        "background": [{"properties": {"show": literal}}],
        "title": [
            {
                "properties": {
                    "text": {"expr": {"Literal": {"Value": "'" + title.replace("'", "''") + "'"}}}
                }
            }
        ],
    }


def _tmdl_type(value: str) -> str:
    return {
        "integer": "int64",
        "number": "double",
        "date": "dateTime",
        "datetime": "dateTime",
        "boolean": "boolean",
        "string": "string",
    }[value]


def _m_type(value: str) -> str:
    return {
        "integer": "Int64.Type",
        "number": "type number",
        "date": "type date",
        "datetime": "type datetime",
        "boolean": "type logical",
        "string": "type text",
    }[value]


def _m_string(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _quote_name(value: str) -> str:
    if not value.strip() or not value.isprintable():
        raise SemanticProjectError("Semantic names must be nonempty printable single-line values")
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        return value
    return "'" + value.replace("'", "''") + "'"


def _identifier(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized:
        normalized = "powerbi_project"
    if not normalized[0].isalpha():
        normalized = "p_" + normalized
    return normalized[:80]


def _safe_part(value: str) -> str:
    return re.sub(r"[<>:\"/\\|?*]+", "_", value).strip(" .")[:100] or "Table"


def _platform_properties(
    display_name: str,
    project_id: str,
    *,
    artifact_type: str,
) -> dict[str, object]:
    logical_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"ai-powerbi-analyst:{project_id}:{artifact_type.casefold()}",
    )
    return {
        "$schema": (
            "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
            "platformProperties/2.0.0/schema.json"
        ),
        "metadata": {"type": artifact_type, "displayName": display_name},
        "config": {"version": "2.0", "logicalId": str(logical_id)},
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _validator_issues(argv: tuple[str, ...], label: str) -> list[str]:
    issues, schemas_unreachable = _validator_attempt(argv, label)
    if schemas_unreachable and label == "Microsoft PBIR validator":
        # The CLI's schema cache lasts only for its process. Retry once with the same
        # validation command; a missing schema is never treated as a validation pass.
        issues, _schemas_unreachable = _validator_attempt(argv, label)
    return issues


def _validator_attempt(argv: tuple[str, ...], label: str) -> tuple[list[str], bool]:
    try:
        result = subprocess.run(  # noqa: S603 - pinned validator argv; shell=False
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=90,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [f"{label} could not run: {error}"], False
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:500]
        return [f"{label} failed: {detail or 'unknown error'}"], False
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [f"{label} returned invalid JSON"], False
    if label == "pbi-cli":
        if not isinstance(value, dict) or value.get("valid") is not True:
            return [f"{label} rejected the report"], False
        summary = value.get("summary")
        if isinstance(summary, dict) and summary.get("warnings", 0):
            return [f"{label} reported warnings"], False
        return [], False
    data = value.get("data") if isinstance(value, dict) else None
    diagnostics = data.get("diagnostics") if isinstance(data, dict) else None
    error_count = data.get("errorCount") if isinstance(data, dict) else None
    if (
        isinstance(data, dict)
        and isinstance(diagnostics, dict)
        and diagnostics
        and set(diagnostics) <= {"PBIR_SCHEMA_UNREACHABLE"}
        and isinstance(error_count, int)
        and not isinstance(error_count, bool)
        and error_count == 0
    ):
        # Still fails closed, but names the cause: the validator downloads
        # Microsoft's JSON schemas and could not reach them.
        return (
            [
                f"{label} could not download Microsoft's report schemas, so the report was "
                "not schema-checked. Check the internet connection and build again."
            ],
            True,
        )
    if not isinstance(data, dict) or data.get("result") != "succeeded":
        return [f"{label} rejected the report"], False
    if data.get("errorCount", 0) or data.get("warningCount", 0):
        return [f"{label} reported errors or warnings"], False
    return [], False
