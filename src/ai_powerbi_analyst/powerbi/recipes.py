"""Fixed report recipes compiled from closed application contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Literal

from ai_powerbi_analyst.contracts import (
    DimensionDefinition,
    MetricDefinition,
    ReportChangePlan,
    ReportPagePlan,
    SemanticCatalog,
    VisualRecipe,
)


class RecipeError(ValueError):
    """A plan cannot be represented by the approved recipe library."""


BindingRole = Literal["category", "value", "row", "field", "legend", "indicator", "goal"]
BindingKind = Literal["measure", "column"]


@dataclass(frozen=True, slots=True)
class FieldBinding:
    role: BindingRole
    reference: str
    kind: BindingKind


@dataclass(frozen=True, slots=True)
class VisualPosition:
    x: int
    y: int
    width: int
    height: int
    z: int
    tab_order: int


@dataclass(frozen=True, slots=True)
class VisualBlueprint:
    logical_key: str
    visual_id: str
    recipe: VisualRecipe
    visual_type: str
    title: str
    position: VisualPosition
    bindings: tuple[FieldBinding, ...] = ()
    is_evidence_slot: bool = False


@dataclass(frozen=True, slots=True)
class PageBlueprint:
    key: str
    page_id: str
    title: str
    width: int
    height: int
    visuals: tuple[VisualBlueprint, ...]


@dataclass(frozen=True, slots=True)
class DashboardBlueprint:
    pages: tuple[PageBlueprint, ...]
    style_preset: str
    catalog_version: str

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


APPROVED_PAGE_RECIPES: dict[str, frozenset[VisualRecipe]] = {
    "executive_pulse": frozenset(
        {
            VisualRecipe.KPI_ROW,
            VisualRecipe.MONTHLY_TREND,
            VisualRecipe.CATEGORY_BAR,
            VisualRecipe.SLICER_ROW,
        }
    ),
    "margin_drivers": frozenset(
        {
            VisualRecipe.KPI_ROW,
            VisualRecipe.CONTRIBUTION_BRIDGE,
            VisualRecipe.CATEGORY_BAR,
            VisualRecipe.DETAIL_MATRIX,
            VisualRecipe.EVIDENCE_SLOT,
        }
    ),
    "outlook_decisions": frozenset(
        {
            VisualRecipe.KPI_ROW,
            VisualRecipe.FORECAST_LINE,
            VisualRecipe.SCENARIO_SUMMARY,
        }
    ),
}


class RecipeCompiler:
    """Turn approved semantic IDs into deterministic page/visual blueprints."""

    def compile(
        self,
        plan: ReportChangePlan,
        catalog: SemanticCatalog,
    ) -> DashboardBlueprint:
        if plan.catalog_version != catalog.version:
            raise RecipeError(
                f"catalog version mismatch: plan={plan.catalog_version}, catalog={catalog.version}"
            )
        page_keys = [page.key for page in plan.pages]
        if len(page_keys) != len(set(page_keys)):
            raise RecipeError("duplicate page key")

        pages = tuple(self._compile_page(page, catalog) for page in plan.pages)
        logical_keys = [visual.logical_key for page in pages for visual in page.visuals]
        if len(logical_keys) != len(set(logical_keys)):
            raise RecipeError("recipe produced duplicate logical visual keys")
        return DashboardBlueprint(
            pages=pages,
            style_preset=plan.style_preset,
            catalog_version=catalog.version,
        )

    def replace_evidence_slot(
        self,
        blueprint: DashboardBlueprint,
        *,
        catalog: SemanticCatalog,
        metric_id: str,
        dimension_id: str,
        chart_kind: Literal["bar", "line", "table"],
    ) -> DashboardBlueprint:
        """Replace the one reserved slot while preserving its stable ID and bounds."""

        if blueprint.catalog_version != catalog.version:
            raise RecipeError("evidence blueprint and semantic catalog versions do not match")
        if (metric_id, dimension_id, chart_kind) != (
            "driver_contribution_west",
            "driver",
            "bar",
        ):
            raise RecipeError("only the approved West driver bridge can replace the evidence slot")
        visual_type = "clusteredBarChart"
        replacement_bindings = (
            self._report_object_binding(
                catalog,
                "driver",
                role="category",
                expected_kind="column",
                expected_source="driver",
            ),
            self._report_object_binding(
                catalog,
                "driver_contribution_west",
                role="value",
                expected_kind="measure",
                expected_source="driver",
            ),
        )
        slot_count = sum(
            visual.is_evidence_slot for page in blueprint.pages for visual in page.visuals
        )
        if slot_count != 1:
            raise RecipeError("blueprint must contain exactly one reserved evidence slot")

        updated_pages: list[PageBlueprint] = []
        for page in blueprint.pages:
            updated_visuals = tuple(
                replace(
                    visual,
                    visual_type=visual_type,
                    title="West margin change · governed contributions",
                    bindings=replacement_bindings,
                )
                if visual.is_evidence_slot
                else visual
                for visual in page.visuals
            )
            updated_pages.append(replace(page, visuals=updated_visuals))
        return replace(blueprint, pages=tuple(updated_pages))

    def _compile_page(
        self,
        page: ReportPagePlan,
        catalog: SemanticCatalog,
    ) -> PageBlueprint:
        approved = APPROVED_PAGE_RECIPES.get(page.key)
        if approved is None:
            raise RecipeError(f"unsupported page key: {page.key}")
        if len(page.visual_recipes) != len(set(page.visual_recipes)):
            raise RecipeError(f"duplicate visual recipe on page {page.key}")
        unsupported = set(page.visual_recipes).difference(approved)
        if unsupported:
            names = ", ".join(sorted(value.value for value in unsupported))
            raise RecipeError(f"unsupported recipe for {page.key}: {names}")

        try:
            metrics = [catalog.resolve_metric(metric_id) for metric_id in page.metric_ids]
        except KeyError as exc:
            raise RecipeError(str(exc).strip("'")) from exc
        try:
            dimensions = [catalog.resolve_dimension(dim_id) for dim_id in page.dimension_ids]
        except KeyError as exc:
            raise RecipeError(str(exc).strip("'")) from exc

        if not metrics:
            raise RecipeError(f"page {page.key} needs at least one approved metric")
        page_id = _stable_id(f"page:{page.key}")
        visuals: list[VisualBlueprint] = []

        for recipe in page.visual_recipes:
            visuals.extend(
                self._compile_recipe(
                    page.key,
                    recipe,
                    catalog=catalog,
                    metrics=metrics,
                    dimensions=dimensions,
                    starting_z=len(visuals),
                )
            )

        for index, left in enumerate(visuals):
            for right in visuals[index + 1 :]:
                if _positions_overlap(left.position, right.position):
                    raise RecipeError(
                        f"visual recipes overlap on {page.key}: "
                        f"{left.logical_key} and {right.logical_key}"
                    )

        return PageBlueprint(
            key=page.key,
            page_id=page_id,
            title=page.title,
            width=1280,
            height=720,
            visuals=tuple(visuals),
        )

    def _compile_recipe(
        self,
        page_key: str,
        recipe: VisualRecipe,
        *,
        catalog: SemanticCatalog,
        metrics: list[MetricDefinition],
        dimensions: list[DimensionDefinition],
        starting_z: int,
    ) -> list[VisualBlueprint]:
        metric_refs = [(metric.id, metric.powerbi_ref) for metric in metrics]
        dimension_refs = [
            (dimension.id, dimension.powerbi_ref, dimension.data_type) for dimension in dimensions
        ]

        if recipe is VisualRecipe.KPI_ROW:
            if page_key == "margin_drivers":
                card_specs = [
                    (
                        object_id,
                        title,
                        self._report_object_binding(
                            catalog,
                            object_id,
                            role="field",
                            expected_kind="measure",
                            expected_source="driver",
                        ),
                    )
                    for object_id, title in (
                        ("driver_net_sales_change_pct", "Latest quarter Net Sales change"),
                        ("driver_gross_profit_change", "Latest quarter Gross Profit change"),
                        ("driver_gross_margin_change_pp", "Gross Margin change"),
                        ("driver_discount_contribution", "Discount contribution"),
                    )
                ]
            elif page_key == "outlook_decisions":
                card_specs = [
                    (
                        object_id,
                        title,
                        self._report_object_binding(
                            catalog,
                            object_id,
                            role="field",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                    )
                    for object_id, title in (
                        ("next_quarter_forecast_net_sales", "Next-quarter Net Sales"),
                        ("next_quarter_forecast_gross_profit", "Next-quarter Gross Profit"),
                        ("forecast_provenance", "Forecast provenance"),
                    )
                ]
            elif page_key == "executive_pulse":
                card_specs = [
                    (
                        object_id,
                        title,
                        self._report_object_binding(
                            catalog,
                            object_id,
                            role="field",
                            expected_kind="measure",
                            expected_source="overview",
                        ),
                    )
                    for object_id, title in (
                        ("overview_latest_quarter_net_sales", "2025 Q4 Net Sales"),
                        (
                            "overview_latest_quarter_gross_profit",
                            "2025 Q4 Gross Profit",
                        ),
                        (
                            "overview_latest_quarter_gross_margin_pct",
                            "2025 Q4 Gross Margin",
                        ),
                        (
                            "overview_latest_quarter_sales_growth",
                            "Net Sales growth vs 2024 Q4",
                        ),
                    )
                ]
            else:
                metric_names = {metric.id: metric.name for metric in metrics}
                card_specs = [
                    (
                        metric_id,
                        metric_names[metric_id],
                        FieldBinding("field", reference, "measure"),
                    )
                    for metric_id, reference in metric_refs[:4]
                ]
            width = (1216 - (len(card_specs) - 1) * 16) // len(card_specs)
            y = 32 if page_key != "outlook_decisions" else 548
            return [
                self._visual(
                    f"{page_key}.kpi.{object_id}",
                    recipe,
                    "cardVisual",
                    title,
                    VisualPosition(
                        32 + index * (width + 16),
                        y,
                        width,
                        112,
                        starting_z + index,
                        starting_z + index,
                    ),
                    (binding,),
                )
                for index, (object_id, title, binding) in enumerate(card_specs)
            ]

        if recipe is VisualRecipe.FORECAST_LINE:
            return [
                self._visual(
                    f"{page_key}.forecast",
                    recipe,
                    "lineChart",
                    "Approved six-month outlook · 80% and 95% uncertainty",
                    VisualPosition(32, 64, 760, 420, starting_z, starting_z),
                    (
                        self._report_object_binding(
                            catalog,
                            "forecast_period",
                            role="category",
                            expected_kind="column",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_net_sales_lower_95",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_net_sales_upper_95",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_net_sales",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_net_sales_lower_80",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_net_sales_upper_80",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                        self._report_object_binding(
                            catalog,
                            "forecast_gross_profit",
                            role="value",
                            expected_kind="measure",
                            expected_source="forecast",
                        ),
                    ),
                )
            ]

        if recipe is VisualRecipe.MONTHLY_TREND:
            time_dimension = next((item for item in dimension_refs if item[2] == "date"), None)
            if time_dimension is None:
                raise RecipeError(f"{recipe.value} requires an approved date dimension")
            visuals = [
                self._visual(
                    f"{page_key}.monthly_trend",
                    recipe,
                    "lineChart",
                    "Monthly Net Sales trend",
                    VisualPosition(32, 176, 760, 320, starting_z, starting_z),
                    (
                        FieldBinding("category", time_dimension[1], "column"),
                        FieldBinding("value", metric_refs[0][1], "measure"),
                    ),
                )
            ]
            if page_key == "executive_pulse":
                visuals.append(
                    self._visual(
                        f"{page_key}.computed_finding",
                        recipe,
                        "cardVisual",
                        "Computed finding · latest complete quarter",
                        VisualPosition(
                            680,
                            520,
                            568,
                            160,
                            starting_z + 1,
                            starting_z + 1,
                        ),
                        (
                            self._report_object_binding(
                                catalog,
                                "overview_executive_finding",
                                role="field",
                                expected_kind="measure",
                                expected_source="overview",
                            ),
                        ),
                    )
                )
            return visuals

        if recipe is VisualRecipe.CONTRIBUTION_BRIDGE:
            return [
                self._visual(
                    f"{page_key}.contribution_bridge",
                    recipe,
                    "clusteredBarChart",
                    "Gross Profit change by governed driver",
                    VisualPosition(32, 176, 760, 300, starting_z, starting_z),
                    (
                        self._report_object_binding(
                            catalog,
                            "driver",
                            role="category",
                            expected_kind="column",
                            expected_source="driver",
                        ),
                        self._report_object_binding(
                            catalog,
                            "driver_contribution_all",
                            role="value",
                            expected_kind="measure",
                            expected_source="driver",
                        ),
                    ),
                )
            ]

        if recipe is VisualRecipe.CATEGORY_BAR:
            category = next((item for item in dimension_refs if item[2] != "date"), None)
            if category is None:
                raise RecipeError(f"{recipe.value} requires an approved categorical dimension")
            if page_key == "executive_pulse":
                logical_name = "category_bar"
                position = VisualPosition(816, 176, 432, 320, starting_z, starting_z)
                value_binding = self._report_object_binding(
                    catalog,
                    "overview_latest_quarter_net_sales",
                    role="value",
                    expected_kind="measure",
                    expected_source="overview",
                )
            else:
                logical_name = "category_bar"
                position = VisualPosition(32, 500, 760, 180, starting_z, starting_z)
                value_binding = FieldBinding("value", metric_refs[0][1], "measure")
            return [
                self._visual(
                    f"{page_key}.{logical_name}",
                    recipe,
                    "clusteredBarChart",
                    "2025 Q4 regional performance"
                    if page_key == "executive_pulse"
                    else "Performance",
                    position,
                    (
                        FieldBinding("category", category[1], "column"),
                        value_binding,
                    ),
                )
            ]

        if recipe is VisualRecipe.DETAIL_MATRIX:
            if not dimension_refs:
                raise RecipeError("detail_matrix requires an approved dimension")
            matrix_bindings = [
                FieldBinding("row", reference, "column") for _, reference, _ in dimension_refs[:2]
            ]
            title = "Category and region detail"
            if page_key == "margin_drivers":
                matrix_bindings.extend(
                    self._report_object_binding(
                        catalog,
                        object_id,
                        role="value",
                        expected_kind="measure",
                        expected_source="overview",
                    )
                    for object_id in (
                        "overview_latest_quarter_net_sales",
                        "overview_latest_quarter_gross_profit",
                        "overview_latest_quarter_gross_margin_pct",
                    )
                )
                title = "2025 Q4 category and region detail"
            else:
                matrix_bindings.extend(
                    FieldBinding("value", ref, "measure") for _, ref in metric_refs[:3]
                )
            return [
                self._visual(
                    f"{page_key}.detail_matrix",
                    recipe,
                    "pivotTable",
                    title,
                    VisualPosition(816, 176, 432, 300, starting_z, starting_z),
                    tuple(matrix_bindings),
                )
            ]

        if recipe is VisualRecipe.EVIDENCE_SLOT:
            return [
                self._visual(
                    f"{page_key}.evidence_slot",
                    recipe,
                    "textbox",
                    "Conversational evidence",
                    VisualPosition(32, 500, 1216, 180, starting_z, starting_z),
                    (),
                    is_evidence_slot=True,
                )
            ]

        if recipe is VisualRecipe.SCENARIO_SUMMARY:
            scenario_bindings = tuple(
                self._report_object_binding(
                    catalog,
                    object_id,
                    role="field",
                    expected_kind="measure",
                    expected_source="scenario",
                )
                for object_id in (
                    "scenario_net_sales",
                    "scenario_units",
                    "scenario_gross_profit",
                    "scenario_gross_margin_pct",
                    "scenario_assumption",
                )
            )
            return [
                self._visual(
                    f"{page_key}.scenario_summary",
                    recipe,
                    "cardVisual",
                    "Approved +3% price scenario · mechanical, not a prediction",
                    VisualPosition(816, 64, 432, 420, starting_z, starting_z),
                    scenario_bindings,
                )
            ]

        if recipe is VisualRecipe.SLICER_ROW:
            selected_slicers = dimension_refs[:3]
            if not selected_slicers:
                raise RecipeError("slicer_row requires at least one approved dimension")
            return [
                self._visual(
                    f"{page_key}.slicer.{dimension_id}",
                    recipe,
                    "slicer",
                    f"Filter by {dimension_id.replace('_', ' ').title()}",
                    VisualPosition(
                        32 + index * 216,
                        520,
                        200,
                        160,
                        starting_z + index,
                        starting_z + index,
                    ),
                    (FieldBinding("field", reference, "column"),),
                )
                for index, (dimension_id, reference, _) in enumerate(selected_slicers)
            ]

        raise RecipeError(f"recipe is not implemented: {recipe.value}")

    @staticmethod
    def _report_object_binding(
        catalog: SemanticCatalog,
        object_id: str,
        *,
        role: BindingRole,
        expected_kind: BindingKind,
        expected_source: Literal["overview", "driver", "forecast", "scenario"],
    ) -> FieldBinding:
        try:
            report_object = catalog.resolve_report_object(object_id)
        except KeyError as exc:
            raise RecipeError(str(exc).strip("'")) from exc
        if report_object.kind != expected_kind or report_object.source != expected_source:
            raise RecipeError(
                f"report object {object_id} must be a {expected_source} {expected_kind}"
            )
        return FieldBinding(role, report_object.powerbi_ref, report_object.kind)

    def _visual(
        self,
        logical_key: str,
        recipe: VisualRecipe,
        visual_type: str,
        title: str,
        position: VisualPosition,
        bindings: tuple[FieldBinding, ...],
        *,
        is_evidence_slot: bool = False,
    ) -> VisualBlueprint:
        return VisualBlueprint(
            logical_key=logical_key,
            visual_id=_stable_id(f"visual:{logical_key}"),
            recipe=recipe,
            visual_type=visual_type,
            title=title,
            position=position,
            bindings=bindings,
            is_evidence_slot=is_evidence_slot,
        )


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _positions_overlap(left: VisualPosition, right: VisualPosition) -> bool:
    return (
        left.x < right.x + right.width
        and right.x < left.x + left.width
        and left.y < right.y + right.height
        and right.y < left.y + left.height
    )
