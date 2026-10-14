export type Health = {
  status: "ready" | "degraded";
  version: string;
  state_root: string;
  model_available: boolean;
  model_name: string;
  model_digest?: string;
  available_memory_gb: number;
  total_memory_gb?: number | null;
  ollama_version?: string;
  model_runtime?: string;
  model_error?: string;
  powerbi_desktop_available: boolean;
  powerbi_desktop_path?: string;
  powerbi_desktop_version?: string;
  pbi_cli_version?: string;
  authoring_cli_version?: string;
  desktop_bridge_version?: string;
  offline: true;
};

export type Workspace = {
  id: string;
  name: string;
  kind: "new" | "existing_pbip" | "imported_report";
  imported_report_id?: string;
  selected_folder?: string;
  existing_pbip?: string;
  active_snapshot_id?: string;
  active_draft_id?: string;
  chat_boundaries?: number[];
  base_hash: string;
};

export type InventoryItem = {
  id: string;
  name: string;
  relative_path: string;
  absolute_path: string;
  size_bytes: number;
  modified_at: string;
  detected_format: "csv" | "xls" | "xlsx" | "xlsm" | "parquet" | "pbip" | "unsupported";
  selectable: boolean;
  warning?: string;
};

export type Compatibility = {
  version: "1.0";
  pbip_path: string;
  supported: boolean;
  project_hash?: string;
  connection_mode: "import" | "direct_query" | "dual" | "hybrid" | "direct_lake" | "live" | "composite" | "unknown";
  storage_modes?: ("import" | "direct_query" | "dual" | "hybrid" | "direct_lake" | "unknown")[];
  blocked_features: string[];
  notes: string[];
};

export type ColumnProfile = {
  id: string;
  name: string;
  data_type: string;
  row_count: number;
  null_count: number;
  distinct_count: number;
  distinct_count_exact?: boolean;
  candidate_key: boolean;
  sample_values: Array<string | number | boolean>;
};

export type TableProfile = {
  id: string;
  name: string;
  row_count: number;
  columns: ColumnProfile[];
};

export type DatabaseSnapshotSource = { provider: "snowflake" | "sqlserver"; profile: string; schema_name: string; tables: string[]; max_rows?: number };

export type Snapshot = {
  database_source?: DatabaseSnapshotSource;
  id: string;
  workspace_id: string;
  created_at: string;
  original_bytes: number;
  normalized_bytes: number;
  tables: Array<{ id: string; table: TableProfile; original_sha256: string; normalized_sha256: string }>;
  findings: Array<{ id: string; severity: string; code: string; message: string; table_id?: string; column_id?: string }>;
  base_hash: string;
};

export type Relationship = {
  id: string;
  from_table_id: string;
  from_column_id: string;
  to_table_id: string;
  to_column_id: string;
  cardinality: "many_to_one" | "one_to_one" | "many_to_many";
  cross_filter_direction: "single" | "both";
  is_active: boolean;
  rely_on_referential_integrity: boolean;
  confidence: number;
  evidence: string[];
  blockers: string[];
  review_status: "pending" | "approved" | "rejected";
  recommended_by_model: boolean;
};

export type GovernedMeasure = {
  tier: "governed";
  id: string;
  name: string;
  operation: "sum" | "count" | "distinct_count" | "average" | "minimum" | "maximum" | "ratio" | "margin" | "weighted_average" | "running_total" | "period_to_date" | "period_change" | "variance";
  table_id: string;
  column_id?: string;
  weight_column_id?: string;
  date_column_id?: string;
  base_measure_id?: string;
  numerator_measure_id?: string;
  denominator_measure_id?: string;
  period: "month" | "quarter" | "year";
  format_string: string;
};

export type AdvancedDaxMeasure = {
  tier: "advanced_dax";
  id: string;
  name: string;
  table_id: string;
  expression: string;
  dependencies: string[];
  format_string: string;
  assumptions: string[];
  extra_approval_required: true;
  approved: boolean;
};

export type Measure = GovernedMeasure | AdvancedDaxMeasure;

export type Visual = {
  date_filter?: { table_id: string; column_id: string; start?: string | null; end?: string | null } | null;
  forecast?: { forecast_length: number; confidence: 75 | 80 | 85 | 90 | 95 | 99; ignore_last: number; seasonality?: number | null } | null;
  id: string;
  recipe: "kpi" | "trend" | "category" | "detail";
  title: string;
  visual_type: string;
  table_id: string;
  measure_id?: string;
  category_column_id?: string;
  detail_column_ids: string[];
  x: number;
  y: number;
  width: number;
  height: number;
};

export type ReportAction = { kind: "visual_date_filter"; page_id?: string; visual_id?: string };

export type Draft = {
  id: string;
  workspace_id: string;
  snapshot_id: string;
  prompt: string;
  understanding: {
    summary: string;
    suggested_questions: string[];
    caveats: string[];
  };
  semantic_model: {
    approved: boolean;
    tables: Array<{
      table_id: string;
      role: "fact" | "dimension" | "bridge" | "detail";
      display_name: string;
      hidden_column_ids: string[];
    }>;
    relationships: Relationship[];
  };
  measure_proposal: {
    measures: Measure[];
  };
  pages: Array<{ version: "1.0"; id: string; title: string; purpose: string; visuals: Visual[]; recommended_relationship_ids: string[] }>;
  previews: Array<{ visual_id: string; query_receipt_id?: string; query_plan_hash?: string; query_result_hash?: string; query_elapsed_ms?: number; rows: Array<Record<string, unknown>>; vega_lite_spec: Record<string, unknown>; empty_reason?: "missing_measure" | "advanced_dax" | "pending_relationship" | "unsupported_operation" | "aggregate_plan_error" | "empty_result" | null }>;
  status: "draft" | "approved" | "building" | "validated" | "failed";
  base_hash: string;
  updated_at?: string;
  planning_provenance?: {
    narrative?: "local_model" | "deterministic_compiler";
    layout_template?: string;
    layout_template_source?: "local_model" | "prompt_rule" | "default";
    report_theme?: string;
    report_theme_source?: "local_model" | "prompt_rule" | "default" | "user";
  };
};

export type ReportThemePalette = {
  id: string;
  label: string;
  use_when: string;
  data_colors: string[];
  background: string;
  foreground: string;
};

export type UpdateDashboardRequest = {
  workspace_id: string;
  pages: Draft["pages"];
  idempotency_key: string;
  expected_base_hash: string;
};

export type AdvancedDaxReviewRequest = {
  workspace_id: string;
  draft_id: string;
  measure_id: string;
  approved: boolean;
  approval_note: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type DraftRevisionReview = {
  prompt: string;
  added_measures: string[];
  changed_measures: string[];
  removed_measures: string[];
  added_pages: string[];
  changed_pages: string[];
  removed_pages: string[];
  before: { measures: number; pages: number; visuals: number };
  after: { measures: number; pages: number; visuals: number };
  approval_reset: boolean;
};

export type Build = {
  id: string;
  workspace_id: string;
  draft_id: string;
  state: "queued" | "compiling" | "validating" | "committed" | "failed" | "rolled_back";
  message: string;
  project_path?: string;
  project_hash?: string;
  created_at: string;
  updated_at: string;
};

export type ValidationReceipt = {
  id: string;
  created_at: string;
  passed: boolean;
  project_hash?: string;
  validators: string[];
  issues: Array<{ source?: string; severity?: string; code?: string; message: string; path?: string }>;
};

export type RelinkBuildRequest = {
  workspace_id: string;
  expected_project_hash: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type StudioEvent = {
  sequence: number;
  type: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type OpenResult = {
  opened: boolean;
  path?: string;
  desktop_executable?: string;
  data_refreshed?: boolean;
  render_verified?: boolean;
  cache_saved?: boolean;
  refreshed_tables?: string[];
  screenshot_paths?: string[];
  reason?: string;
};

export type ExportReceipt = {
  id: string;
  build_id: string;
  created_at: string;
  exported_project_path: string;
  project_hash: string;
};

export type UndoReceipt = {
  id: string;
  created_at: string;
  backup_id: string;
  restored: boolean;
  restored_hash?: string;
};

export type Message = {
  id?: number;
  role: "user" | "assistant" | "system";
  content: string;
  citations: string[];
  created_at: string;
  status?: "completed" | "interrupted" | "cancelled" | "failed";
  request_id?: string;
};

export type ClearChatRequest = {
  workspace_id: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type SetWorkspaceFolderRequest = {
  workspace_id: string;
  folder: string;
  name?: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type RenameWorkspaceRequest = {
  workspace_id: string;
  name: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type DeleteWorkspaceRequest = {
  workspace_id: string;
  confirm_name: string;
  idempotency_key: string;
  expected_base_hash: string;
};

export type WorkspaceRemoval = {
  id: string;
  name: string;
  source_folder?: string | null;
  existing_pbip?: string | null;
  removed_snapshots: number;
  removed_drafts: number;
  removed_builds: number;
  removed_messages: number;
  removed_events: number;
  files_untouched: boolean;
  deleted_at: string;
};

export type BrowseEntry = {
  name: string;
  path: string;
  kind: "dir" | "file";
  selectable: boolean;
};

export type BrowseResult = {
  path: string;
  parent: string | null;
  entries: BrowseEntry[];
  truncated: boolean;
};

export type DaxDatePreview = { column_id: string; grain: "day" | "month" | "quarter" | "year" };
export type DaxDateField = { column_id: string; name: string; table: string };
export type DaxGroupValue = string | number | boolean | null;
export type DaxPreviewContext = { column_id: string; grain?: DaxDatePreview["grain"] | null; search?: string };
export type DaxPreviewField = DaxDateField & { data_type: string };
export type DaxResult = {
  engine?: "snapshot" | "powerbi";
  grand_total_column?: string | null;
  columns: string[];
  rows: Array<Record<string, string | number | boolean | null>>;
  row_count: number;
  truncated: boolean;
  elapsed_ms: number;
  plan: string;
  date_fields?: DaxDateField[];
  date_preview?: { column: string; value_column: string; field: DaxDateField; grain: DaxDatePreview["grain"] } | null;
  preview_fields?: DaxPreviewField[];
  breakdown?: { column: string; value_column: string; field: DaxPreviewField; grain?: DaxDatePreview["grain"] | null } | null;
};

export type AddDaxMeasureRequest = {
  workspace_id: string;
  name: string;
  expression: string;
  format_string?: string;
  idempotency_key: string;
  expected_base_hash: string;
};
