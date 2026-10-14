"""Catalog-grounded dashboard planning with a deterministic safe fallback."""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field

from ai_powerbi_analyst.contracts import (
    ClosedModel,
    DashboardPagePlan,
    DashboardPlan,
    DslMeasureSpec,
    MeasureSpec,
    RelationshipCandidate,
    SemanticModelPlan,
    SemanticTablePlan,
    SourceManifest,
    VisualPlan,
    VisualRoleBinding,
    VisualTypeCapability,
)
from ai_powerbi_analyst.llm.runtime import StructuredModelRuntime


class _SuggestedBinding(ClosedModel):
    role: str = Field(min_length=1, max_length=80)
    field: str = Field(min_length=1, max_length=200)


class _SuggestedVisual(ClosedModel):
    visual_type: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=120)
    bindings: list[_SuggestedBinding] = Field(default_factory=list, max_length=12)


class _SuggestedPage(ClosedModel):
    title: str = Field(min_length=1, max_length=80)
    visuals: list[_SuggestedVisual] = Field(min_length=1, max_length=8)


class _SuggestedDashboard(ClosedModel):
    pages: list[_SuggestedPage] = Field(min_length=1, max_length=4)
    assumptions: list[str] = Field(default_factory=list, max_length=8)


class _SuggestedPageBlueprint(ClosedModel):
    title: str = Field(min_length=1, max_length=80)
    primary_measure: str = Field(min_length=1, max_length=200)
    time_field: str | None = Field(default=None, max_length=200)
    category_field: str | None = Field(default=None, max_length=200)
    detail_fields: list[str] = Field(default_factory=list, max_length=4)


def default_visual_catalog() -> tuple[VisualTypeCapability, ...]:
    return (
        VisualTypeCapability(
            visual_type="cardVisual",
            display_name="Card",
            roles=["Data"],
            role_kinds={"Data": "Measure"},
            required_roles=["Data"],
            source="built_in_fallback",
        ),
        VisualTypeCapability(
            visual_type="lineChart",
            display_name="Line chart",
            roles=["Category", "Y"],
            role_kinds={"Category": "Grouping", "Y": "Measure"},
            required_roles=["Category", "Y"],
            source="built_in_fallback",
        ),
        VisualTypeCapability(
            visual_type="clusteredColumnChart",
            display_name="Clustered column chart",
            roles=["Category", "Y"],
            role_kinds={"Category": "Grouping", "Y": "Measure"},
            required_roles=["Category", "Y"],
            source="built_in_fallback",
        ),
        VisualTypeCapability(
            visual_type="tableEx",
            display_name="Table",
            roles=["Values"],
            role_kinds={"Values": "GroupingOrMeasure"},
            required_roles=["Values"],
            source="built_in_fallback",
        ),
    )


class DashboardPlanningService:
    def __init__(
        self,
        model: StructuredModelRuntime | None,
        visual_catalog: tuple[VisualTypeCapability, ...] | None = None,
    ) -> None:
        self.model = model
        self.visual_catalog = visual_catalog or default_visual_catalog()

    def plan(
        self,
        prompt: str,
        manifest: SourceManifest,
        relationships: list[RelationshipCandidate],
    ) -> DashboardPlan:
        semantic = self._semantic_plan(manifest, relationships)
        suggested = self._model_suggestion(prompt, manifest, semantic)
        generation_source: Literal["local_model", "deterministic_fallback"] = "local_model"
        if suggested is None:
            suggested = self._fallback_suggestion(manifest, semantic)
            generation_source = "deterministic_fallback"
        return DashboardPlan(
            prompt=prompt,
            semantic_model=semantic,
            pages=self._compile_pages(suggested, manifest, semantic),
            assumptions=[
                *suggested.assumptions,
                "Relationships are applied only after explicit review.",
                "Dashboard values remain bound to the versioned local source snapshot.",
            ],
            generation_source=generation_source,
        )

    def _semantic_plan(
        self,
        manifest: SourceManifest,
        relationships: list[RelationshipCandidate],
    ) -> SemanticModelPlan:
        measures: list[MeasureSpec] = []
        for table in manifest.tables:
            for column in table.columns:
                if column.data_type.value not in {"integer", "number"}:
                    continue
                measures.append(
                    DslMeasureSpec(
                        id=_id(f"total_{table.id}_{column.id}"),
                        name=f"Total {column.name}",
                        operation="sum",
                        table_id=table.id,
                        column_id=column.id,
                        format_string="#,#0.00",
                    )
                )
                if len(measures) >= 20:
                    break
            if len(measures) >= 20:
                break
        approved_relationships = [
            relationship
            for relationship in relationships
            if relationship.review_status == "approved" and not relationship.blockers
        ]
        has_date = any(
            column.data_type.value in {"date", "datetime"}
            for table in manifest.tables
            for column in table.columns
        )
        return SemanticModelPlan(
            manifest_import_id=manifest.import_id,
            tables=[
                SemanticTablePlan(table_id=table.id, display_name=table.name)
                for table in manifest.tables
            ],
            relationships=approved_relationships,
            measures=measures,
            create_calendar=has_date,
        )

    def _model_suggestion(
        self,
        prompt: str,
        manifest: SourceManifest,
        semantic: SemanticModelPlan,
    ) -> _SuggestedDashboard | None:
        if self.model is None or not self.model.health().model_available:
            return None
        fields = [
            {
                "field": f"{table.id}.{column.id}",
                "name": column.name,
                "type": column.data_type.value,
            }
            for table in manifest.tables
            for column in table.columns
        ]
        fields.extend(
            {"field": f"measure.{measure.id}", "name": measure.name, "type": "measure"}
            for measure in semantic.measures
        )
        payload = json.dumps(
            {
                "untrusted_user_request": prompt,
                "approved_fields": fields,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
        try:
            blueprint = self.model.generate(
                """Choose fields for one neutral analytical dashboard page. Use supplied field
IDs exactly. primary_measure must be a measure field. Use a date field for time_field, a
text field for category_field, and up to four approved fields for detail_fields. Use null
when a suitable optional field does not exist. Never invent IDs or figures.""",
                payload,
                _SuggestedPageBlueprint,
                max_output_tokens=160,
                timeout_seconds=40,
            )
            return self._compile_blueprint(blueprint, fields)
        except Exception:  # noqa: BLE001 - blueprint failures fall back to deterministic planning
            return None

    def _compile_blueprint(
        self,
        blueprint: _SuggestedPageBlueprint,
        fields: list[dict[str, str]],
    ) -> _SuggestedDashboard | None:
        allowed = {item["field"]: item for item in fields}
        primary = allowed.get(blueprint.primary_measure)
        if primary is None or primary["type"] != "measure":
            return None
        time_field = allowed.get(blueprint.time_field or "")
        if time_field is not None and time_field["type"] not in {"date", "datetime"}:
            time_field = None
        category = allowed.get(blueprint.category_field or "")
        if category is not None and category["type"] != "string":
            category = None
        details = [field for field in blueprint.detail_fields if field in allowed][:4]
        visuals = [
            _SuggestedVisual(
                visual_type="cardVisual",
                title=primary["name"],
                bindings=[_SuggestedBinding(role="Data", field=blueprint.primary_measure)],
            )
        ]
        if time_field is not None:
            visuals.append(
                _SuggestedVisual(
                    visual_type="lineChart",
                    title=f"{primary['name']} over time",
                    bindings=[
                        _SuggestedBinding(role="Category", field=time_field["field"]),
                        _SuggestedBinding(role="Y", field=blueprint.primary_measure),
                    ],
                )
            )
        if category is not None:
            visuals.append(
                _SuggestedVisual(
                    visual_type="clusteredColumnChart",
                    title=f"{primary['name']} by {category['name']}",
                    bindings=[
                        _SuggestedBinding(role="Category", field=category["field"]),
                        _SuggestedBinding(role="Y", field=blueprint.primary_measure),
                    ],
                )
            )
        detail_bindings = [
            _SuggestedBinding(role="Values", field=field)
            for field in dict.fromkeys([*details, blueprint.primary_measure])
        ]
        visuals.append(
            _SuggestedVisual(
                visual_type="tableEx",
                title="Source detail",
                bindings=detail_bindings,
            )
        )
        return _SuggestedDashboard(
            pages=[_SuggestedPage(title=blueprint.title, visuals=visuals)],
            assumptions=["The local model selected approved fields for this page."],
        )

    def _fallback_suggestion(
        self,
        manifest: SourceManifest,
        semantic: SemanticModelPlan,
    ) -> _SuggestedDashboard:
        if not semantic.measures:
            table = manifest.tables[0]
            value_field = f"{table.id}.{table.columns[0].id}"
        else:
            value_field = f"measure.{semantic.measures[0].id}"
        dimensions = [
            f"{table.id}.{column.id}"
            for table in manifest.tables
            for column in table.columns
            if column.data_type.value in {"string", "date", "datetime"}
        ]
        date_dimension = next(
            (
                f"{table.id}.{column.id}"
                for table in manifest.tables
                for column in table.columns
                if column.data_type.value in {"date", "datetime"}
            ),
            None,
        )
        category = next((item for item in dimensions if item != date_dimension), None)
        visuals = [
            _SuggestedVisual(
                visual_type="cardVisual",
                title="Primary measure",
                bindings=[_SuggestedBinding(role="Data", field=value_field)],
            )
        ]
        if date_dimension:
            visuals.append(
                _SuggestedVisual(
                    visual_type="lineChart",
                    title="Primary measure over time",
                    bindings=[
                        _SuggestedBinding(role="Category", field=date_dimension),
                        _SuggestedBinding(role="Y", field=value_field),
                    ],
                )
            )
        if category:
            visuals.append(
                _SuggestedVisual(
                    visual_type="clusteredColumnChart",
                    title="Primary measure by category",
                    bindings=[
                        _SuggestedBinding(role="Category", field=category),
                        _SuggestedBinding(role="Y", field=value_field),
                    ],
                )
            )
        visuals.append(
            _SuggestedVisual(
                visual_type="tableEx",
                title="Source detail",
                bindings=[
                    *(
                        [
                            _SuggestedBinding(
                                role="Values",
                                field=category or date_dimension or value_field,
                            )
                        ]
                        if category or date_dimension
                        else []
                    ),
                    _SuggestedBinding(role="Values", field=value_field),
                ],
            )
        )
        return _SuggestedDashboard(pages=[_SuggestedPage(title="Overview", visuals=visuals)])

    def _compile_pages(
        self,
        suggestion: _SuggestedDashboard,
        manifest: SourceManifest,
        semantic: SemanticModelPlan,
    ) -> list[DashboardPagePlan]:
        column_fields = {
            f"{table.id}.{column.id}": (table.id, column.id)
            for table in manifest.tables
            for column in table.columns
        }
        measure_fields = {
            f"measure.{measure.id}": (
                measure.table_id if isinstance(measure, DslMeasureSpec) else manifest.tables[0].id,
                measure.id,
            )
            for measure in semantic.measures
        }
        allowed_visuals = {
            item.visual_type: set(item.roles)
            for item in self.visual_catalog
            if item.offline_capable
        }
        pages: list[DashboardPagePlan] = []
        for page_index, page in enumerate(suggestion.pages):
            visuals: list[VisualPlan] = []
            for visual_index, visual in enumerate(page.visuals):
                allowed_roles = allowed_visuals.get(visual.visual_type)
                if allowed_roles is None:
                    continue
                bindings: list[VisualRoleBinding] = []
                for suggested_binding in visual.bindings:
                    if suggested_binding.role not in allowed_roles:
                        continue
                    field = column_fields.get(suggested_binding.field)
                    field_kind: Literal["column", "measure"] = "column"
                    if field is None:
                        field = measure_fields.get(suggested_binding.field)
                        field_kind = "measure"
                    if field is None:
                        continue
                    bindings.append(
                        VisualRoleBinding(
                            role=suggested_binding.role,
                            table_id=field[0],
                            field_id=field[1],
                            field_kind=field_kind,
                        )
                    )
                if visual.bindings and not bindings:
                    continue
                row = visual_index // 2
                column = visual_index % 2
                width = 292 if visual.visual_type == "cardVisual" else 588
                visuals.append(
                    VisualPlan(
                        id=_id(f"visual_{page_index + 1}_{visual_index + 1}"),
                        visual_type=visual.visual_type,
                        title=visual.title,
                        bindings=bindings,
                        x=32 + column * 612,
                        y=32 + row * 224,
                        width=width,
                        height=176 if visual.visual_type == "cardVisual" else 200,
                    )
                )
            if visuals:
                pages.append(
                    DashboardPagePlan(
                        id=_id(f"page_{page_index + 1}_{page.title}"),
                        title=page.title,
                        visuals=visuals,
                    )
                )
        if not pages:
            raise ValueError("Dashboard plan contained no valid visual bindings")
        return pages


def _id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = "item_" + normalized
    return normalized[:96]
