"""Versioned closed contracts for the native local-AI studio."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from ai_powerbi_analyst.contracts import (
    ClosedModel,
    ColumnProfile,
    ReportThemeId,
    Sha256,
    TableProfile,
    VisualDateFilter,
    VisualForecast,
)

StudioId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]


class WorkspaceKind(StrEnum):
    NEW = "new"
    EXISTING_PBIP = "existing_pbip"
    IMPORTED_REPORT = "imported_report"


class LocalWorkspace(ClosedModel):
    version: Literal["1.0"] = "1.0"
    id: StudioId
    name: str = Field(min_length=1, max_length=120)
    kind: WorkspaceKind = WorkspaceKind.NEW
    selected_folder: str | None = None
    existing_pbip: str | None = None
    imported_report_id: str | None = None
    active_snapshot_id: StudioId | None = None
    active_draft_id: StudioId | None = None
    chat_boundaries: tuple[int, ...] = ()
    base_hash: Sha256
    created_at: datetime
    updated_at: datetime


class SourceInventoryItem(ClosedModel):
    id: StudioId
    name: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)
    absolute_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    detected_format: Literal["csv", "xls", "xlsx", "xlsm", "parquet", "pbip", "unsupported"]
    selectable: bool
    warning: str | None = None


class IngestionSelection(ClosedModel):
    version: Literal["1.0"] = "1.0"
    workspace_id: StudioId
    inventory_item_ids: list[str] = Field(min_length=1, max_length=200)


class DataQualityFinding(ClosedModel):
    id: StudioId
    severity: Literal["info", "warning", "error"]
    code: Literal[
        "empty_table",
        "duplicate_columns",
        "mixed_types",
        "null_key",
        "duplicate_key",
        "locale_number",
        "mixed_dates",
        "external_links_ignored",
        "macros_ignored",
    ]
    table_id: str | None = None
    column_id: str | None = None
    message: str = Field(min_length=1, max_length=500)


class NormalizedTableSnapshot(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    source_item_id: StudioId
    original_sha256: Sha256
    original_snapshot_path: str = Field(min_length=1)
    normalized_sha256: Sha256
    normalized_parquet_path: str = Field(min_length=1)
    table: TableProfile


class DatabaseSnapshotSource(ClosedModel):
    provider: Literal["snowflake", "sqlserver"]
    profile: str
    schema_name: str
    tables: list[str]
    max_rows: int = Field(default=100_000, ge=1, le=1_000_000)


class DatasetSnapshot(ClosedModel):
    version: Literal["1.0"] = "1.0"
    id: StudioId
    workspace_id: StudioId
    created_at: datetime
    original_bytes: int = Field(ge=0)
    normalized_bytes: int = Field(ge=0)
    tables: list[NormalizedTableSnapshot] = Field(min_length=1)
    findings: list[DataQualityFinding] = Field(default_factory=list)
    manifest_path: str = Field(min_length=1)
    base_hash: Sha256
    database_source: DatabaseSnapshotSource | None = None


class AggregateFilter(ClosedModel):
    column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    operator: Literal["eq", "neq", "gt", "gte", "lt", "lte", "contains"]
    value: str | int | float | bool


AggregateOperand = Literal["sum", "average", "minimum", "maximum", "count", "distinct_count"]


class ReadOnlyAggregatePlan(ClosedModel):
    version: Literal["1.0"] = "1.0"
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    operation: Literal[
        "sum", "average", "minimum", "maximum", "count", "distinct_count", "ratio", "difference"
    ]
    value_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    denominator_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    # Aggregates on each side of a ratio/difference. None keeps the original
    # SUM meaning, so DIVIDE(SUM(Sales), DISTINCTCOUNT(Order ID)) is expressible.
    numerator_operation: AggregateOperand | None = None
    denominator_operation: AggregateOperand | None = None
    group_by_column_ids: list[str] = Field(default_factory=list, max_length=3)
    time_bucket: Literal["day", "week", "month", "quarter", "year"] | None = None
    filters: list[AggregateFilter] = Field(default_factory=list, max_length=8)
    # Extra filters on a ratio/difference numerator only, so a share such as
    # DIVIDE(CALCULATE(SUM(Sales), Region = "West"), SUM(Sales)) is expressible.
    numerator_filters: list[AggregateFilter] = Field(default_factory=list, max_length=8)
    top_n: int = Field(default=20, ge=1, le=100)
    timeout_seconds: int = Field(default=12, ge=1, le=15)

    @model_validator(mode="after")
    def operation_has_required_value(self) -> ReadOnlyAggregatePlan:
        combined = self.operation in {"ratio", "difference"}
        if (
            self.operation != "count"
            and self.value_column_id is None
            and not (combined and self.numerator_operation == "count")
        ):
            raise ValueError("aggregate operation requires a value column")
        if (
            combined
            and self.denominator_column_id is None
            and self.denominator_operation != "count"
        ):
            raise ValueError("ratio/difference requires a second column")
        if not combined and (
            self.numerator_operation or self.denominator_operation or self.numerator_filters
        ):
            raise ValueError("operand aggregates apply to ratio/difference only")
        return self


class QueryReceipt(ClosedModel):
    id: StudioId
    created_at: datetime
    plan_hash: Sha256
    result_hash: Sha256
    row_count: int = Field(ge=0, le=100)
    elapsed_ms: int = Field(ge=0)
    rows: list[dict[str, str | int | float | bool | None]] = Field(max_length=100)


class RelationshipProposal(ClosedModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    from_table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    from_column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    to_table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    to_column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    cardinality: Literal["many_to_one", "one_to_one", "many_to_many"]
    cross_filter_direction: Literal["single", "both"] = "single"
    is_active: bool = True
    rely_on_referential_integrity: bool = False
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(min_length=1, max_length=6)
    blockers: list[str] = Field(default_factory=list, max_length=6)
    review_status: Literal["pending", "approved", "rejected"] = "pending"
    recommended_by_model: bool = False


class DatasetUnderstandingPlan(ClosedModel):
    version: Literal["1.0"] = "1.0"
    summary: str = Field(min_length=1, max_length=900)
    grains: dict[str, str] = Field(default_factory=dict, max_length=30)
    date_field_ids: list[str] = Field(default_factory=list, max_length=20)
    numeric_field_ids: list[str] = Field(default_factory=list, max_length=50)
    categorical_field_ids: list[str] = Field(default_factory=list, max_length=50)
    suggested_questions: list[str] = Field(default_factory=list, max_length=6)
    caveats: list[str] = Field(default_factory=list, max_length=8)


# Must match studio/layout_templates.json; a unit test enforces the pairing.
LayoutTemplateId = Literal[
    "classic_overview",
    "ranked_performance",
    "kpi_rail_trend",
    "driver_analysis",
    "comparison_grid",
    "operations_monitor",
    "executive_scorecard",
]


class DatasetNarrativePlan(ClosedModel):
    """The only model-authored portion of initial dashboard planning.

    ``layout_template_id`` and ``report_theme_id`` name reviewed page structures
    and hash-pinned themes; deterministic code still binds every field and places
    every visual.
    """

    # Prose about a profile gains nothing from DAX/M worked examples, and on CPU
    # every prompt token costs prefill time inside the bounded narrative call.
    worked_examples: ClassVar[bool] = False
    # Constrained decoding may skip optional properties; require the design keys in
    # the generation schema (null stays allowed) while Python callers keep defaults.
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra=lambda schema: schema.update(
            required=["layout_template_id", "report_theme_id", "summary"]
        ),
    )

    version: Literal["1.0"] = "1.0"
    # Design choices come first so constrained decoding settles them before prose.
    layout_template_id: LayoutTemplateId | None = None
    report_theme_id: ReportThemeId | None = None
    summary: str = Field(min_length=1, max_length=700)
    suggested_questions: list[str] = Field(default_factory=list, max_length=5)
    caveats: list[str] = Field(default_factory=list, max_length=6)


class InitialPlanningProvenance(ClosedModel):
    """Stage-level authorship for the bounded initial planning pipeline."""

    version: Literal["1.0"] = "1.0"
    narrative: Literal["local_model", "deterministic_compiler"] = "deterministic_compiler"
    semantic_model: Literal["deterministic_compiler"] = "deterministic_compiler"
    measures: Literal["deterministic_compiler"] = "deterministic_compiler"
    pages: Literal["deterministic_compiler"] = "deterministic_compiler"
    previews: Literal["deterministic_compiler"] = "deterministic_compiler"
    layout_template: LayoutTemplateId = "classic_overview"
    layout_template_source: Literal["local_model", "prompt_rule", "default"] = "default"
    # The reviewed theme is a build input; it lives here so every draft hash covers it.
    report_theme: ReportThemeId = "default"
    report_theme_source: Literal["local_model", "prompt_rule", "default", "user"] = "default"


class SemanticTableProposal(ClosedModel):
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    role: Literal["fact", "dimension", "bridge", "detail"]
    display_name: str = Field(min_length=1, max_length=120)
    hidden_column_ids: list[str] = Field(default_factory=list, max_length=100)


class SemanticModelProposal(ClosedModel):
    version: Literal["1.0"] = "1.0"
    tables: list[SemanticTableProposal] = Field(min_length=1)
    relationships: list[RelationshipProposal] = Field(default_factory=list)
    calendar_required: bool = False
    approved: bool = False


class GovernedMeasureSpec(ClosedModel):
    tier: Literal["governed"] = "governed"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
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
        default="#,#0.00", min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$"
    )

    def _validate_required_inputs(self) -> None:
        if (
            self.operation
            in {
                "sum",
                "distinct_count",
                "average",
                "minimum",
                "maximum",
            }
            and self.column_id is None
        ):
            raise ValueError(f"{self.operation} requires a column")
        if self.operation == "weighted_average" and (
            self.column_id is None or self.weight_column_id is None
        ):
            raise ValueError("weighted_average requires value and weight columns")
        if self.operation in {"ratio", "margin", "variance"} and (
            self.numerator_measure_id is None or self.denominator_measure_id is None
        ):
            raise ValueError(f"{self.operation} requires numerator and denominator measures")
        if self.operation in {"running_total", "period_to_date", "period_change"} and (
            self.base_measure_id is None or self.date_column_id is None
        ):
            raise ValueError(f"{self.operation} requires a base measure and date column")

    @model_validator(mode="after")
    def operation_has_required_inputs(self) -> GovernedMeasureSpec:
        self._validate_required_inputs()
        return self


class AdvancedDaxProposal(ClosedModel):
    tier: Literal["advanced_dax"] = "advanced_dax"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=120)
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    expression: str = Field(min_length=1, max_length=4000)
    dependencies: list[str] = Field(default_factory=list, max_length=20)
    format_string: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[^\r\n]+$",
    )
    assumptions: list[str] = Field(min_length=1, max_length=8)
    extra_approval_required: Literal[True] = True
    approved: bool = False


class MeasureProposal(ClosedModel):
    version: Literal["1.0"] = "1.0"
    measures: list[GovernedMeasureSpec | AdvancedDaxProposal] = Field(
        default_factory=list, max_length=40
    )


VisualTypeId = Literal[
    "cardVisual",
    "lineChart",
    "areaChart",
    "stackedAreaChart",
    "clusteredColumnChart",
    "clusteredBarChart",
    "barChart",
    "columnChart",
    "pieChart",
    "donutChart",
    "funnel",
    "waterfallChart",
    "tableEx",
]


class DashboardVisualProposal(ClosedModel):
    date_filter: VisualDateFilter | None = None
    forecast: VisualForecast | None = None
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    recipe: Literal["kpi", "trend", "category", "detail"]
    title: str = Field(min_length=1, max_length=120)
    visual_type: VisualTypeId
    table_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    measure_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    category_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    detail_column_ids: list[str] = Field(default_factory=list, max_length=8)
    x: int = Field(ge=0, le=12)
    y: int = Field(ge=0, le=12)
    width: int = Field(ge=1, le=12)
    height: int = Field(ge=1, le=12)

    @model_validator(mode="after")
    def forecast_requires_bound_trend(self) -> DashboardVisualProposal:
        if self.forecast is not None and (
            self.recipe != "trend"
            or self.visual_type != "lineChart"
            or self.measure_id is None
            or self.category_column_id is None
        ):
            raise ValueError(
                "A forecast requires a lineChart trend with a date category and measure"
            )
        return self


class DashboardPageProposal(ClosedModel):
    version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    title: str = Field(min_length=1, max_length=80)
    purpose: str = Field(min_length=1, max_length=300)
    visuals: list[DashboardVisualProposal] = Field(min_length=1, max_length=12)


class PreviewDataset(ClosedModel):
    visual_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    query_receipt_id: str | None = None
    query_plan_hash: Sha256 | None = None
    query_result_hash: Sha256 | None = None
    query_elapsed_ms: int | None = Field(default=None, ge=0)
    rows: list[dict[str, str | int | float | bool | None]] = Field(
        default_factory=list, max_length=100
    )
    vega_lite_spec: dict[str, object] = Field(default_factory=dict)
    empty_reason: (
        Literal[
            "missing_measure",
            "advanced_dax",
            "pending_relationship",
            "unsupported_operation",
            "aggregate_plan_error",
            "empty_result",
        ]
        | None
    ) = None


class DashboardDraft(ClosedModel):
    version: Literal["1.0"] = "1.0"
    id: StudioId
    workspace_id: StudioId
    snapshot_id: StudioId
    prompt: str = Field(min_length=1, max_length=2000)
    understanding: DatasetUnderstandingPlan
    planning_provenance: InitialPlanningProvenance = Field(
        default_factory=InitialPlanningProvenance
    )
    semantic_model: SemanticModelProposal
    measure_proposal: MeasureProposal
    pages: list[DashboardPageProposal] = Field(min_length=1, max_length=8)
    previews: list[PreviewDataset] = Field(default_factory=list)
    status: Literal["draft", "approved", "building", "validated", "failed"] = "draft"
    base_hash: Sha256
    created_at: datetime
    updated_at: datetime


class ProjectCompatibilityReport(ClosedModel):
    version: Literal["1.0"] = "1.0"
    pbip_path: str
    supported: bool
    project_hash: Sha256 | None = None
    connection_mode: Literal[
        "import", "direct_query", "dual", "hybrid", "direct_lake", "live", "composite", "unknown"
    ]
    storage_modes: list[
        Literal["import", "direct_query", "dual", "hybrid", "direct_lake", "unknown"]
    ] = Field(default_factory=list)
    blocked_features: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ProjectChangeSet(ClosedModel):
    id: StudioId
    workspace_id: StudioId
    draft_id: StudioId
    expected_base_hash: Sha256
    component_paths: list[str] = Field(default_factory=list)
    approved: bool = False


class ApprovalRecord(ClosedModel):
    id: StudioId
    workspace_id: StudioId
    object_type: Literal[
        "relationship", "semantic_model", "advanced_dax", "dashboard", "project_write"
    ]
    object_id: str
    approved: bool
    object_hash: Sha256
    created_at: datetime


class StudioBuildState(StrEnum):
    QUEUED = "queued"
    COMPILING = "compiling"
    VALIDATING = "validating"
    COMMITTED = "committed"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class BuildJob(ClosedModel):
    version: Literal["1.0"] = "1.0"
    id: StudioId
    workspace_id: StudioId
    draft_id: StudioId
    state: StudioBuildState
    message: str = ""
    project_path: str | None = None
    project_hash: Sha256 | None = None
    created_at: datetime
    updated_at: datetime


class ValidationReceipt(ClosedModel):
    id: StudioId
    build_id: StudioId
    created_at: datetime
    passed: bool
    project_hash: Sha256 | None = None
    validators: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class BackupReceipt(ClosedModel):
    id: StudioId
    created_at: datetime
    original_project_path: str
    backup_path: str
    project_hash: Sha256
    retained: bool = True


class UndoReceipt(ClosedModel):
    id: StudioId
    created_at: datetime
    backup_id: str
    restored: bool
    restored_hash: Sha256 | None = None


class IdempotentMutation(ClosedModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_base_hash: Sha256


class CreateWorkspaceRequest(IdempotentMutation):
    name: str = Field(min_length=1, max_length=120)
    selected_folder: str | None = None
    existing_pbip: str | None = None


class InventoryRequest(ClosedModel):
    folder: str = Field(min_length=1)


class CompatibilityRequest(ClosedModel):
    pbip_path: str = Field(min_length=1)


class IngestRequest(IdempotentMutation):
    workspace_id: StudioId
    item_paths: list[str] = Field(min_length=1, max_length=200)


class DraftRequest(IdempotentMutation):
    workspace_id: StudioId
    snapshot_id: StudioId
    prompt: str = Field(min_length=1, max_length=2000)


class PageVisualAddition(ClosedModel):
    """Append one reviewed visual to a named existing page, never replace its contents."""

    page_id: str = Field(min_length=1, max_length=96)
    visual: DashboardVisualProposal
    placement: Literal["below_existing"] = "below_existing"


class DraftRevisionProposal(ClosedModel):
    """Strict local-model output containing additions only."""

    version: Literal["1.0"] = "1.0"
    measures: list[GovernedMeasureSpec | AdvancedDaxProposal] = Field(
        default_factory=list,
        max_length=8,
    )
    pages: list[DashboardPageProposal] = Field(default_factory=list, max_length=3)
    visual_additions: list[PageVisualAddition] = Field(default_factory=list, max_length=1)
    recommended_relationship_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def contains_a_revision(self) -> DraftRevisionProposal:
        if not any(
            (self.measures, self.pages, self.visual_additions, self.recommended_relationship_ids)
        ):
            raise ValueError("a draft revision must add a measure, page, or relationship review")
        return self


class ModelGovernedMeasureSpec(GovernedMeasureSpec):
    """Governed measure as the local model writes it.

    A small model often omits ``date_column_id`` from a YoY measure and no repair fits the
    remaining context, so parsing tolerates that one gap. The planner fills it only when the
    base table has exactly one reviewed date column, then revalidates the strict contract.
    """

    @model_validator(mode="after")
    def operation_has_required_inputs(self) -> ModelGovernedMeasureSpec:
        if self.operation in {"running_total", "period_to_date", "period_change"}:
            if self.base_measure_id is None:
                raise ValueError(f"{self.operation} requires a base measure")
            return self
        self._validate_required_inputs()
        return self


# Advanced DAX has its own planning workflow. Offering that branch in the general revision
# schema let constrained decoding spend the whole time budget writing DAX that is always
# rejected, so the local model sees governed DSL measures only.
class GovernedRevisionProposal(DraftRevisionProposal):
    """Strict local-model output containing governed additions only."""

    # Unrelated M/DAX worked examples would double a compact revision prompt.
    worked_examples: ClassVar[bool] = False
    measures: list[ModelGovernedMeasureSpec] = Field(  # type: ignore[assignment]
        default_factory=list,
        max_length=8,
    )


class ModelRevisionVisual(ClosedModel):
    """One new-page visual as the local model writes it.

    The planner owns IDs, geometry and purpose, and derives the table from the measure,
    so a CPU model spends its output tokens on choices only.
    """

    recipe: Literal["kpi", "trend", "category", "detail"]
    visual_type: VisualTypeId
    title: str = Field(min_length=1, max_length=80)
    measure_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    category_column_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    # Needed only for a detail table or a grouping column that lives in a joined table.
    table_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*$")
    detail_column_ids: list[str] = Field(default_factory=list, max_length=8)


class ModelRevisionPage(ClosedModel):
    title: str = Field(min_length=1, max_length=80)
    visuals: list[ModelRevisionVisual] = Field(min_length=1, max_length=4)


class ModelRevisionProposal(ClosedModel):
    """The model-facing revision schema; the planner expands it to DraftRevisionProposal.

    Visuals for existing pages go through the separate visual selector and date filters
    through the report date-filter action, so neither is offered here.
    """

    worked_examples: ClassVar[bool] = False
    measures: list[ModelGovernedMeasureSpec] = Field(default_factory=list, max_length=4)
    pages: list[ModelRevisionPage] = Field(default_factory=list, max_length=1)
    recommended_relationship_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def contains_a_revision(self) -> ModelRevisionProposal:
        if not any((self.measures, self.pages, self.recommended_relationship_ids)):
            raise ValueError("a draft revision must add a measure, page, or relationship review")
        return self


class ModelRelationshipRevisionProposal(ClosedModel):
    """Join-only model output: recommend profiled candidates, never other draft items."""

    worked_examples: ClassVar[bool] = False
    recommended_relationship_ids: list[str] = Field(min_length=1, max_length=20)


class ReviseDraftRequest(IdempotentMutation):
    workspace_id: StudioId
    prompt: str = Field(min_length=1, max_length=2000)
    from_conversation: bool = False


class UpdateDashboardRequest(IdempotentMutation):
    workspace_id: StudioId
    pages: list[DashboardPageProposal] = Field(min_length=1, max_length=8)


class ApproveDraftRequest(IdempotentMutation):
    workspace_id: StudioId
    approved_relationship_ids: list[str] = Field(default_factory=list)


class ApproveAdvancedDaxRequest(IdempotentMutation):
    workspace_id: StudioId
    draft_id: StudioId
    measure_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    approved: bool
    approval_note: str = Field(min_length=1, max_length=500)


class BuildRequest(IdempotentMutation):
    workspace_id: StudioId
    draft_id: StudioId
    project_name: str = Field(min_length=1, max_length=120)


class OpenBuildRequest(IdempotentMutation):
    workspace_id: StudioId


class ExportBuildRequest(IdempotentMutation):
    workspace_id: StudioId
    destination_folder: str = Field(min_length=1)


class RelinkBuildRequest(IdempotentMutation):
    workspace_id: StudioId
    expected_project_hash: Sha256


class ExportReceipt(ClosedModel):
    id: StudioId
    build_id: StudioId
    created_at: datetime
    exported_project_path: str
    project_hash: Sha256


class UndoBuildRequest(IdempotentMutation):
    workspace_id: StudioId


class ConversationContext(ClosedModel):
    page_id: str | None = Field(default=None, min_length=1, max_length=120)
    visual_id: str | None = Field(default=None, min_length=1, max_length=120)
    table_id: str | None = Field(default=None, min_length=1, max_length=120)
    measure_id: str | None = Field(default=None, min_length=1, max_length=120)
    relationship_id: str | None = Field(default=None, min_length=1, max_length=120)


class RestoreDraftRequest(IdempotentMutation):
    workspace_id: StudioId
    restore_hash: Sha256


class ChatRequest(ClosedModel):
    workspace_id: StudioId
    message: str = Field(min_length=1, max_length=4000)
    context: ConversationContext | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    expected_base_hash: Sha256 | None = None
    # Conversation epochs are separate from the report/data optimistic hash.
    chat_boundary: int | None = Field(default=None, ge=0, strict=True)


class ClearChatRequest(IdempotentMutation):
    workspace_id: StudioId


class ClearDaxChatRequest(ClearChatRequest):
    snapshot_id: StudioId | None = None


class SetWorkspaceFolderRequest(IdempotentMutation):
    workspace_id: StudioId
    folder: str = Field(min_length=1, max_length=1000)
    name: str | None = Field(default=None, min_length=1, max_length=120)


class RenameWorkspaceRequest(IdempotentMutation):
    workspace_id: StudioId
    name: str = Field(min_length=1, max_length=120)


class DeleteWorkspaceRequest(IdempotentMutation):
    """Deleting is irreversible, so the stored name must be echoed back."""

    workspace_id: StudioId
    confirm_name: str = Field(min_length=1, max_length=120)


class WorkspaceRemoval(ClosedModel):
    """Receipt for a deleted workspace.

    It records what the local engine stopped storing. ``files_untouched`` is
    always true: the selected data folder, any exported PBIP copy and every
    Power BI Desktop project outside the state root are left exactly as they
    were.
    """

    version: Literal["1.0"] = "1.0"
    id: StudioId
    name: str = Field(min_length=1, max_length=120)
    source_folder: str | None = None
    existing_pbip: str | None = None
    removed_snapshots: int = Field(ge=0)
    removed_drafts: int = Field(ge=0)
    removed_builds: int = Field(ge=0)
    removed_messages: int = Field(ge=0)
    removed_events: int = Field(ge=0)
    files_untouched: bool = True
    deleted_at: datetime


class BrowseEntry(ClosedModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    kind: Literal["dir", "file"]
    selectable: bool = False


class BrowseResult(ClosedModel):
    path: str = ""
    parent: str | None = None
    entries: list[BrowseEntry] = Field(default_factory=list)
    truncated: bool = False


class DaxPreviewContext(ClosedModel):
    column_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    grain: Literal["day", "month", "quarter", "year"] | None = None
    search: str = Field(default="", max_length=100)


class DaxDateField(ClosedModel):
    column_id: str
    name: str
    table: str


class DaxPreviewField(DaxDateField):
    data_type: str


class DaxGroupSelection(ClosedModel):
    value: Annotated[str, Field(max_length=4000)] | int | float | bool | None


class DaxBreakdownResult(ClosedModel):
    column: str
    value_column: str
    field: DaxPreviewField
    grain: Literal["day", "month", "quarter", "year"] | None = None


class DaxDateResult(ClosedModel):
    column: str
    value_column: str
    field: DaxDateField
    grain: Literal["day", "month", "quarter", "year"]


class DaxEvaluateRequest(ClosedModel):
    workspace_id: StudioId
    expression: str = Field(min_length=1, max_length=4000)
    engine: Literal["snapshot", "powerbi"] = "snapshot"
    top_n: int = Field(default=50, ge=1, le=100)
    preview: DaxPreviewContext | None = None
    detail_period: date | None = None
    detail_group: DaxGroupSelection | None = None

    @model_validator(mode="after")
    def period_requires_preview(self) -> DaxEvaluateRequest:
        if (
            self.detail_period is not None or self.detail_group is not None
        ) and self.preview is None:
            raise ValueError("A group inspection requires a preview")
        if self.detail_period is not None and self.detail_group is not None:
            raise ValueError("Inspect a date period or a category, not both")
        return self


class DaxAssistRequest(ClosedModel):
    workspace_id: StudioId
    # Optional: without a snapshot the assistant is limited to advice/explain
    # turns — formula generation and fixes require schema context.
    snapshot_id: StudioId | None = None
    request_id: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=1000)
    expression: str = Field(default="", max_length=4000)
    error: str = Field(default="", max_length=600)
    mode: Literal["auto", "generate", "explain", "fix"] = "generate"
    discussion: str = Field(default="", max_length=2000)
    chat_boundary: int | None = Field(default=None, ge=0, strict=True)


class DaxResult(ClosedModel):
    engine: Literal["snapshot", "powerbi"] = "snapshot"
    columns: list[str] = Field(max_length=40)
    rows: list[dict[str, str | int | float | bool | None]] = Field(max_length=100)
    row_count: int = Field(ge=0, le=100)
    truncated: bool = False
    elapsed_ms: int = Field(ge=0)
    plan: str = Field(min_length=1, max_length=600)
    grand_total_column: str | None = None
    date_fields: list[DaxDateField] = Field(default_factory=list, max_length=40)
    date_preview: DaxDateResult | None = None
    preview_fields: list[DaxPreviewField] = Field(default_factory=list, max_length=40)
    breakdown: DaxBreakdownResult | None = None


class AddDaxMeasureRequest(IdempotentMutation):
    workspace_id: StudioId
    name: str = Field(min_length=1, max_length=120)
    expression: str = Field(min_length=1, max_length=4000)
    format_string: str = Field(
        default="#,#0.00", min_length=1, max_length=80, pattern=r"^[^\r\n\x00]+$"
    )


class HealthResponse(ClosedModel):
    model_runtime: str = "ollama"
    model_error: str | None = None
    status: Literal["ready", "degraded"]
    version: str
    state_root: str
    model_available: bool
    model_name: str
    model_digest: str | None = None
    available_memory_gb: float
    memory_profile: str | None = None
    startup_memory_gb: float | None = None
    total_memory_gb: float | None = None
    automatic_model_selection: bool = False
    ollama_version: str | None = None
    powerbi_desktop_available: bool = False
    powerbi_desktop_path: str | None = None
    powerbi_desktop_version: str | None = None
    pbi_cli_version: str | None = None
    authoring_cli_version: str | None = None
    desktop_bridge_version: str | None = None
    offline: Literal[True] = True


class TableCatalog(ClosedModel):
    table_id: str
    name: str
    parquet_path: str
    columns: list[ColumnProfile]


# --- editing an imported report (docs/imported-report-editing-api.md) -----------------------

RevisionId = Annotated[str, Field(pattern=r"^r\d{4,}$")]
EditIdempotencyKey = Annotated[str, Field(min_length=8, max_length=200)]
DesktopWorkChoice = Literal["ask", "save"]


class ImportedEditAnswer(ClosedModel):
    """The user's answer to a question an edit raised (``needs_choice``): the option's ``fill``
    is written over the fields of the operation at ``step`` (1-based) before it is applied."""

    step: int = Field(ge=1, le=50)
    fill: dict[str, Any] = Field(min_length=1, max_length=20)


# Confirmations a whole batch answers (not a flag of one operation): ``report_tests_broken``
# lets a model or measure edit go ahead although it changes numbers the user pinned as tests.
BatchConfirmation = Literal["report_tests_broken"]


class ImportedEditRequest(ClosedModel):
    """One batch of typed report operations, applied as a single revision."""

    operations: list[dict[str, Any]] = Field(min_length=1, max_length=50)
    expected_head: RevisionId
    idempotency_key: EditIdempotencyKey
    summary: str | None = Field(default=None, max_length=300)
    desktop_work: DesktopWorkChoice = "ask"
    confirm_replace: bool = False
    confirm_model: bool = False
    answers: list[ImportedEditAnswer] = Field(default_factory=list, max_length=50)
    confirmed: list[BatchConfirmation] = Field(default_factory=list, max_length=4)


class ImportedUndoRequest(ClosedModel):
    expected_head: RevisionId
    idempotency_key: EditIdempotencyKey
    desktop_work: DesktopWorkChoice = "ask"


class ImportedRestoreRequest(ClosedModel):
    revision_id: RevisionId
    expected_head: RevisionId
    idempotency_key: EditIdempotencyKey
    confirmed_discard: bool = False
    desktop_work: DesktopWorkChoice = "ask"


class ImportedSaveCopyRequest(ClosedModel):
    destination: str = Field(min_length=1, max_length=1000)
    idempotency_key: EditIdempotencyKey


class ImportedReplaceOriginalRequest(ClosedModel):
    idempotency_key: EditIdempotencyKey
    confirm: Literal[True]


PendingEditId = Annotated[str, Field(min_length=8, max_length=80)]


class ChatEditChoiceRequest(ClosedModel):
    """The answer to an ``edit_choices`` question of an imported report's chat. Without a key
    the server makes one (the answer is then not replayed when it is sent twice)."""

    pending_id: PendingEditId
    index: int = Field(ge=0, le=99)
    idempotency_key: EditIdempotencyKey | None = None


class ChatEditConfirmRequest(ClosedModel):
    """The answer to an ``edit_confirm`` question: apply it (``accept``) or leave the report."""

    pending_id: PendingEditId
    accept: bool
    idempotency_key: EditIdempotencyKey | None = None


# --- judging an imported report: review, model health, report tests, explain ------------------
# (docs/imported-report-editing-api.md, "Judgement"). The shapes the judgement modules own
# (tests, filters, expectations, the render request) are validated by those modules: they are
# plain objects here, so this module does not import the renderer or the engine.

CellValue = StrictStr | StrictInt | StrictFloat | StrictBool | None
ReportTestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]


class ImportedReviewRequest(ClosedModel):
    """Review one page; ``with_data`` draws it with Studio's engine first, so the rules that
    read rows (empty visuals, pie slices, one-entry legends) run too."""

    with_data: bool = True


class ModelHealthFixRequest(ClosedModel):
    """Apply one model health finding's fix as one edit. ``base_hash`` is the head's hash the
    finding was read on; ``confirmed`` is the user's yes to the fix (it always asks first)."""

    finding_id: str = Field(min_length=1, max_length=300)
    base_hash: Sha256
    confirmed: bool = False
    idempotency_key: EditIdempotencyKey
    desktop_work: DesktopWorkChoice = "ask"


class MeasureName(ClosedModel):
    table: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)


class PinReportTestRequest(ClosedModel):
    """Pin the number a card, a chart point or a table cell shows. ``point`` maps each
    category column's ``query_ref`` to the row's value (empty for a card); ``measure`` picks
    the number when the visual shows several."""

    page_id: str = Field(min_length=1, max_length=200)
    visual_id: str = Field(min_length=1, max_length=200)
    point: dict[str, CellValue] = Field(default_factory=dict, max_length=12)
    value: CellValue
    measure: str | None = Field(default=None, min_length=1, max_length=300)
    name: str | None = Field(default=None, min_length=1, max_length=200)


class AddReportTestRequest(ClosedModel):
    """A test the user writes: a measure or a DAX expression, its filters and what it should
    be (``report_tests.TestFilter`` and ``Expectation`` shapes)."""

    name: str = Field(min_length=1, max_length=200)
    measure: MeasureName | None = None
    expression: str | None = Field(default=None, min_length=1, max_length=4000)
    filters: list[dict[str, Any]] = Field(default_factory=list, max_length=24)
    expect: dict[str, Any]
    format: str | None = Field(default=None, max_length=200)


class UpdateReportTestRequest(ClosedModel):
    """A test's name, expectation or filters; its id (what it computes) never changes."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    expect: dict[str, Any] | None = None
    filters: list[dict[str, Any]] | None = Field(default=None, max_length=24)


class RunReportTestsRequest(ClosedModel):
    """``auto``: Studio's engine first, then Power BI Desktop (only when it already has the
    report open) for the tests the engine could not check."""

    engine: Literal["local", "desktop", "auto"] = "auto"


class ExplainPoint(ClosedModel):
    """One data point: its category values by the column's ``query_ref``, and the value
    column asked about (a measure's ``query_ref`` or name; None: the first shown)."""

    values: dict[str, CellValue] = Field(default_factory=dict, max_length=12)
    measure: str | None = Field(default=None, max_length=300)


class ExplainNumberRequest(ClosedModel):
    """Why a card, a chart point or a table cell shows what it shows. ``canvas`` is the
    ``LiveReportRequest`` the canvas rendered with (selections, bookmark, drill)."""

    page_id: str = Field(min_length=1, max_length=200)
    visual_id: str = Field(min_length=1, max_length=200)
    point: ExplainPoint | None = None
    canvas: dict[str, Any] | None = None
    focus: Literal["value", "colour", "blank"] = "value"
