"""Closed application contracts shared by every subsystem.

The local model may populate these contracts, but it never controls executable
commands, paths, SQL, DAX, PBIR JSON, or layout coordinates.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Scalar = str | int | float | bool
# Must match powerbi/report_themes/registry.json; a unit test enforces the pairing.
ReportThemeId = Literal[
    "default", "executive_light", "calm_teal", "finance_neutral", "accessible_contrast"
]


class ClosedModel(BaseModel):
    """Base contract that rejects undeclared model output."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RouteIntent(StrEnum):
    BUILD = "BUILD"
    MODIFY = "MODIFY"
    ANALYZE = "ANALYZE"
    EXPLAIN = "EXPLAIN"
    FORECAST = "FORECAST"
    SCENARIO = "SCENARIO"
    ADD_EVIDENCE = "ADD_EVIDENCE"
    CLARIFY = "CLARIFY"
    UNSUPPORTED = "UNSUPPORTED"


class AnalysisOperation(StrEnum):
    KPI = "kpi"
    COMPARISON = "comparison"
    TREND = "trend"
    RANKING = "ranking"
    CONTRIBUTION = "contribution"
    DRIVER_BRIDGE = "driver_bridge"
    FORECAST = "forecast"
    SCENARIO = "scenario"


class VisualRecipe(StrEnum):
    KPI_ROW = "kpi_row"
    MONTHLY_TREND = "monthly_trend"
    CATEGORY_BAR = "category_bar"
    CONTRIBUTION_BRIDGE = "contribution_bridge"
    DETAIL_MATRIX = "detail_matrix"
    FORECAST_LINE = "forecast_line"
    SCENARIO_SUMMARY = "scenario_summary"
    EVIDENCE_SLOT = "evidence_slot"
    SLICER_ROW = "slicer_row"


class DimensionDefinition(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    column: str = Field(min_length=1)
    powerbi_ref: str = Field(min_length=1)
    data_type: Literal["string", "integer", "number", "date", "boolean"]


class MetricDefinition(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(min_length=1)
    format: str = Field(min_length=1)
    powerbi_ref: str = Field(min_length=1)
    sql_expression: str = Field(min_length=1)
    allowed_dimensions: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)


class ReportObjectDefinition(ClosedModel):
    """A prepared Power BI field that is never compiled into local SQL."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    powerbi_ref: str = Field(min_length=1)
    kind: Literal["measure", "column"]
    source: Literal["overview", "driver", "forecast", "scenario"]


class SemanticCatalog(ClosedModel):
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    metrics: list[MetricDefinition]
    dimensions: list[DimensionDefinition]
    report_objects: list[ReportObjectDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def aliases_are_unique(self) -> SemanticCatalog:
        metric_keys: dict[str, str] = {}
        for metric in self.metrics:
            for candidate in (metric.id, metric.name, *metric.aliases):
                key = candidate.strip().casefold()
                owner = metric_keys.get(key)
                if owner is not None and owner != metric.id:
                    raise ValueError(f"duplicate metric alias {candidate!r}")
                metric_keys[key] = metric.id

        dimension_keys: dict[str, str] = {}
        for dimension in self.dimensions:
            for candidate in (dimension.id, dimension.name, *dimension.aliases):
                key = candidate.strip().casefold()
                owner = dimension_keys.get(key)
                if owner is not None and owner != dimension.id:
                    raise ValueError(f"duplicate dimension alias {candidate!r}")
                dimension_keys[key] = dimension.id

        object_ids: set[str] = set()
        object_references: set[str] = set()
        governed_ids = {metric.id for metric in self.metrics} | {
            dimension.id for dimension in self.dimensions
        }
        for report_object in self.report_objects:
            if report_object.id in governed_ids or report_object.id in object_ids:
                raise ValueError(f"duplicate semantic object ID {report_object.id!r}")
            if report_object.powerbi_ref in object_references:
                raise ValueError(f"duplicate report-object binding {report_object.powerbi_ref!r}")
            object_ids.add(report_object.id)
            object_references.add(report_object.powerbi_ref)
        return self

    def resolve_metric(self, name_or_id: str) -> MetricDefinition:
        key = name_or_id.strip().casefold()
        for metric in self.metrics:
            candidates = (metric.id, metric.name, *metric.aliases)
            if key in {value.strip().casefold() for value in candidates}:
                return metric
        raise KeyError(f"unknown metric: {name_or_id}")

    def resolve_dimension(self, name_or_id: str) -> DimensionDefinition:
        key = name_or_id.strip().casefold()
        for dimension in self.dimensions:
            candidates = (dimension.id, dimension.name, *dimension.aliases)
            if key in {value.strip().casefold() for value in candidates}:
                return dimension
        raise KeyError(f"unknown dimension: {name_or_id}")

    def resolve_report_object(self, object_id: str) -> ReportObjectDefinition:
        key = object_id.strip().casefold()
        for report_object in self.report_objects:
            if report_object.id.casefold() == key:
                return report_object
        raise KeyError(f"unknown report object: {object_id}")


class ProjectCapabilities(ClosedModel):
    can_analyze: bool = True
    can_explain: bool = True
    can_build: bool = False
    can_modify: bool = False
    can_open_desktop: bool = False
    offline_schema_verified: bool = False
    desktop_render_verified: bool = False
    phase0_kill_test_passed: bool = False
    blockers: list[str] = Field(default_factory=list)


class CompatibilityPins(ClosedModel):
    pbi_cli_version: str = "3.11.1"
    microsoft_validator_version: str = "0.1.4"
    desktop_version: str | None = None
    windows_build: str | None = None
    python_version: str | None = None
    node_version: str | None = None
    pbir_schema_versions: dict[str, str] = Field(default_factory=dict)
    ollama_version: str | None = None
    model_tag: str = "qwen3.5:4b-q4_K_M"
    model_digest: str | None = None


class ProjectBundle(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    name: str = Field(min_length=1)
    pbip_path: Path
    report_path: Path
    semantic_model_path: Path
    duckdb_path: Path
    catalog_path: Path
    data_hash: Sha256
    source_data_hash: Sha256 | None = None
    source_hashes: dict[str, Sha256] = Field(default_factory=dict)
    duckdb_hash: Sha256 | None = None
    catalog_hash: Sha256 | None = None
    pbip_hash: Sha256 | None = None
    report_hash: Sha256 | None = None
    baseline_report_hash: Sha256 | None = None
    baseline_workspace_hash: Sha256 | None = None
    semantic_model_hash: Sha256 | None = None
    phase0_attestation_hash: Sha256 | None = None
    phase0_receipt_hashes: dict[str, Sha256] = Field(default_factory=dict)
    catalog_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    data_through: date
    forecast_version: str = "1.0.0"
    scenario_version: str = "1.0.0"
    baseline_validation_status: Literal["passed", "failed", "pending"] = "pending"
    compatibility: CompatibilityPins = Field(default_factory=CompatibilityPins)
    capabilities: ProjectCapabilities = Field(default_factory=ProjectCapabilities)


class RouteDecision(ClosedModel):
    intent: RouteIntent
    confidence: float = Field(ge=0.0, le=1.0)
    requested_object_ids: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    reason: str = ""


class TimeWindow(ClosedModel):
    start: date | None = None
    end: date | None = None
    preset: (
        Literal[
            "latest_month",
            "latest_quarter",
            "year_to_date",
            "last_12_months",
            "all_time",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def has_explicit_or_preset_window(self) -> TimeWindow:
        explicit = self.start is not None or self.end is not None
        if explicit and self.preset is not None:
            raise ValueError("time window cannot combine explicit dates and a preset")
        if (self.start is None) != (self.end is None):
            raise ValueError("explicit time window requires both start and end")
        return self


class AnalysisPlan(ClosedModel):
    operation: AnalysisOperation
    metric_ids: list[str] = Field(min_length=1, max_length=4)
    dimension_ids: list[str] = Field(default_factory=list, max_length=3)
    time_window: TimeWindow = Field(default_factory=lambda: TimeWindow(preset="latest_quarter"))
    comparison: Literal["prior_period", "prior_year", "none"] = "prior_year"
    filters: dict[str, Scalar] = Field(default_factory=dict)
    ranking_limit: int = Field(default=10, ge=1, le=25)
    row_limit: int = Field(default=200, ge=1, le=200)
    sort_direction: Literal["asc", "desc"] = "desc"
    scenario_price_change_pct: int | None = Field(default=None, ge=-3, le=5)


class ChartSpec(ClosedModel):
    kind: Literal["metric", "line", "bar", "contribution_bar", "table", "forecast"]
    title: str = Field(min_length=1)
    x_field: str | None = None
    y_field: str | None = None
    color_field: str | None = None


class EvidenceVisualPlan(ClosedModel):
    """One fixed, prepared report binding for a grounded evidence answer."""

    binding_id: Literal["west_driver_bridge"]
    metric_object_id: Literal["driver_contribution_west"]
    dimension_object_id: Literal["driver"]
    chart_kind: Literal["bar"] = "bar"
    target_page_key: Literal["margin_drivers"] = "margin_drivers"


class EvidenceAnswer(ClosedModel):
    conclusion: str = Field(min_length=1, max_length=500)
    facts: list[str] = Field(min_length=1, max_length=8)
    metric_ids: list[str] = Field(min_length=1)
    comparison_period: str = Field(min_length=1)
    filters: dict[str, Scalar] = Field(default_factory=dict)
    data_hash: Sha256
    catalog_hash: Sha256
    catalog_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    query_plan_hash: Sha256
    data_through: date | None = None
    metric_definition_refs: list[str] = Field(default_factory=list)
    chart: ChartSpec
    rows: list[dict[str, Scalar | None]] = Field(max_length=200)
    add_to_report_supported: bool = False
    target_page_key: str | None = None
    report_visual_plan: EvidenceVisualPlan | None = None

    @model_validator(mode="after")
    def report_addition_is_explicit(self) -> EvidenceAnswer:
        visual_plan = self.report_visual_plan
        has_plan = visual_plan is not None
        if self.add_to_report_supported != has_plan:
            raise ValueError("report evidence support requires one explicit prepared visual plan")
        if visual_plan is not None and self.target_page_key != visual_plan.target_page_key:
            raise ValueError("evidence target must match its prepared visual plan")
        return self


class ReportPagePlan(ClosedModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1)
    visual_recipes: list[VisualRecipe] = Field(min_length=1)
    metric_ids: list[str] = Field(default_factory=list)
    dimension_ids: list[str] = Field(default_factory=list)


class ReportChangePlan(ClosedModel):
    version: Literal["1.0"] = "1.0"
    base_report_hash: Sha256
    catalog_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    pages: list[ReportPagePlan] = Field(min_length=1, max_length=3)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    style_preset: Literal["executive_light"] = "executive_light"


class SourceKind(StrEnum):
    CSV = "csv"
    EXCEL = "excel"
    PARQUET = "parquet"


class ColumnDataType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


class SourceFile(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    original_name: str = Field(min_length=1)
    original_path: str | None = None
    snapshot_path: str = Field(min_length=1)
    sha256: Sha256
    original_sha256: Sha256 | None = None
    size_bytes: int = Field(ge=0)
    kind: SourceKind
    original_kind: SourceKind | None = None
    sheets: list[str] = Field(default_factory=list)
    encoding: str | None = None
    delimiter: str | None = Field(default=None, min_length=1, max_length=1)


class ColumnProfile(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    data_type: ColumnDataType
    row_count: int = Field(ge=0)
    null_count: int = Field(ge=0)
    distinct_count: int = Field(ge=0)
    distinct_count_exact: bool = True
    candidate_key: bool = False
    sample_values: list[Scalar] = Field(default_factory=list, max_length=8)
    minimum: Scalar | None = None
    maximum: Scalar | None = None
    geographic_role: Literal["country", "state", "city", "postal_code"] | None = None


class TableProfile(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    sheet: str | None = None
    row_count: int = Field(ge=0)
    columns: list[ColumnProfile] = Field(min_length=1)


class SourceManifest(ClosedModel):
    version: Literal["1.0"] = "1.0"
    import_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    imported_at: datetime
    sources: list[SourceFile] = Field(min_length=1)
    tables: list[TableProfile] = Field(min_length=1)


class RelationshipCandidate(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    from_table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    from_column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    to_table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    to_column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    cardinality: Literal["many_to_one", "one_to_one", "many_to_many"]
    cross_filter_direction: Literal["single", "both"] = Field(
        default="single",
        validation_alias=AliasChoices("cross_filter_direction", "cross_filter"),
    )
    is_active: bool = True
    rely_on_referential_integrity: bool = False
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=6)
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    blockers: list[str] = Field(default_factory=list, max_length=6)


class SemanticTablePlan(ClosedModel):
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    hidden_column_ids: list[str] = Field(default_factory=list)


class DslMeasureSpec(ClosedModel):
    tier: Literal["governed"] = "governed"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    operation: Literal[
        "sum",
        "count",
        "distinct_count",
        "average",
        "minimum",
        "maximum",
        "ratio",
        "margin",
        "weighted_average",
        "running_total",
        "period_to_date",
        "period_change",
        "variance",
    ]
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    weight_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    date_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    base_measure_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    numerator_measure_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    denominator_measure_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    period: Literal["month", "quarter", "year"] = "month"
    format_string: str = Field(
        default="#,0.00", min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$"
    )


class ProposedDaxMeasure(ClosedModel):
    tier: Literal["advanced_dax"] = "advanced_dax"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1)
    table_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    expression: str = Field(min_length=1, max_length=4000)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=8)
    format_string: str = Field(
        default="#,#0.00",
        max_length=80,
        pattern=r"^[^\r\n]+$",
    )
    extra_approval_required: Literal[True] = True
    approved: bool = False


MeasureSpec = Annotated[
    DslMeasureSpec | ProposedDaxMeasure,
    Field(discriminator="tier"),
]


class SemanticModelPlan(ClosedModel):
    version: Literal["1.0"] = "1.0"
    manifest_import_id: str = Field(min_length=1)
    tables: list[SemanticTablePlan] = Field(min_length=1)
    relationships: list[RelationshipCandidate] = Field(default_factory=list)
    measures: list[MeasureSpec] = Field(default_factory=list)
    create_calendar: bool = False
    approved: bool = False


class VisualTypeCapability(ClosedModel):
    visual_type: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)
    role_kinds: dict[
        str,
        Literal["Grouping", "Measure", "GroupingOrMeasure"],
    ] = Field(default_factory=dict)
    required_roles: list[str] = Field(default_factory=list)
    max_per_role: dict[str, int] = Field(default_factory=dict)
    formatting_properties: list[str] = Field(default_factory=list)
    offline_capable: bool = True
    validation_status: Literal["catalogued", "roundtrip_passed", "unsupported"] = "catalogued"
    source: Literal["microsoft_catalog", "built_in_fallback"] = "microsoft_catalog"


class VisualRoleBinding(ClosedModel):
    role: str = Field(min_length=1)
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    field_kind: Literal["column", "measure"]


class VisualDateFilter(ClosedModel):
    """An inclusive calendar-date range scoped to one visual's source rows."""

    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    start: date | None = None
    end: date | None = None

    @model_validator(mode="after")
    def valid_range(self) -> VisualDateFilter:
        if self.start is None and self.end is None:
            raise ValueError("Choose a start or end date, or clear the date filter")
        if self.start and self.end and self.start > self.end:
            raise ValueError("Start date must be on or before end date")
        if self.end == date.max:
            raise ValueError("End date must be before 9999-12-31")
        return self


class VisualForecast(ClosedModel):
    """Native Power BI forecast settings; Desktop computes points and confidence bounds."""

    forecast_length: int = Field(default=6, ge=1, le=60, strict=True)
    confidence: Literal[75, 80, 85, 90, 95, 99] = 95
    seasonality: int | None = Field(default=None, ge=1, le=120, strict=True)
    ignore_last: int = Field(default=0, ge=0, le=500, strict=True)


class VisualPlan(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    visual_type: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    bindings: list[VisualRoleBinding] = Field(default_factory=list)
    date_filter: VisualDateFilter | None = None
    forecast: VisualForecast | None = None
    x: int = Field(ge=0, le=1280)
    y: int = Field(ge=0, le=720)
    width: int = Field(gt=0, le=1280)
    height: int = Field(gt=0, le=720)


class DashboardPagePlan(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=80)
    visuals: list[VisualPlan] = Field(min_length=1, max_length=20)


class DashboardPlan(ClosedModel):
    version: Literal["1.0"] = "1.0"
    prompt: str = Field(min_length=1, max_length=2000)
    semantic_model: SemanticModelPlan
    pages: list[DashboardPagePlan] = Field(min_length=1, max_length=8)
    assumptions: list[str] = Field(default_factory=list, max_length=12)
    generation_source: Literal["local_model", "deterministic_fallback"] = "deterministic_fallback"
    report_theme: ReportThemeId = "default"
    approved: bool = False


class BuildJobState(StrEnum):
    PROFILING = "profiling"
    MODELING = "modeling"
    COMPILING = "compiling"
    VALIDATING = "validating"
    DESKTOP_RENDERING = "desktop_rendering"
    COMMITTED = "committed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class BuildJob(ClosedModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    state: BuildJobState
    created_at: datetime
    updated_at: datetime
    project_path: str | None = None
    message: str = ""


class ValidationReceipt(ClosedModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    created_at: datetime
    passed: bool
    project_hash: Sha256 | None = None
    validators: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class UndoReceipt(ClosedModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    created_at: datetime
    before_hash: Sha256
    after_hash: Sha256
    restored: bool
