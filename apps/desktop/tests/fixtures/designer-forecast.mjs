// Synthetic, bounded historical data. This fixture never contains forecast points.
export function designerForecastFixture() {
  const timestamp = "2025-07-01T12:00:00Z";
  const workspace = {
    id: "forecast-regression-workspace",
    name: "Forecast regression fixture",
    kind: "new",
    active_snapshot_id: "forecast-regression-snapshot",
    active_draft_id: "forecast-regression-draft",
    chat_boundaries: [],
    base_hash: "a".repeat(64),
  };
  const column = (id, name, data_type, sample_values) => ({
    id, name, data_type, row_count: 6, null_count: 0,
    distinct_count: sample_values.length, distinct_count_exact: true,
    candidate_key: false, sample_values,
  });
  const historicalRows = [100, 150, 180, 190, 210, 220].map((value, index) => ({
    group_1: `2025-${String(index + 1).padStart(2, "0")}-01`, value,
  }));
  const snapshot = {
    id: workspace.active_snapshot_id,
    workspace_id: workspace.id,
    created_at: timestamp,
    original_bytes: 256,
    normalized_bytes: 256,
    findings: [],
    tables: [{
      id: "snapshot_orders",
      table: {
        id: "orders", name: "Orders", source_id: "orders_csv", row_count: 6,
        columns: [
          column("order_date", "Order Date", "date", historicalRows.map(row => row.group_1)),
          column("region", "Region", "string", ["East", "West"]),
          column("sales", "Sales", "number", historicalRows.map(row => row.value)),
        ],
      },
      original_sha256: "b".repeat(64), normalized_sha256: "b".repeat(64),
    }],
    base_hash: "c".repeat(64),
  };
  const visual = (id, recipe, title, visual_type, binding, placement) => ({
    id, recipe, title, visual_type, table_id: "orders", measure_id: "total_sales",
    detail_column_ids: [], forecast: null, date_filter: null, ...binding, ...placement,
  });
  const draft = {
    id: workspace.active_draft_id,
    workspace_id: workspace.id,
    snapshot_id: snapshot.id,
    prompt: "Review sales over time and by region",
    understanding: {
      summary: "Six months of synthetic orders with date, region and sales.",
      suggested_questions: ["How have sales changed over time?"], caveats: [],
    },
    semantic_model: {
      approved: true,
      tables: [{ table_id: "orders", role: "fact", display_name: "Orders", hidden_column_ids: [] }],
      relationships: [],
    },
    measure_proposal: { measures: [{
      tier: "governed", id: "total_sales", name: "Total Sales", operation: "sum",
      table_id: "orders", column_id: "sales", period: "month", format_string: "#,0.00",
    }] },
    pages: [{
      version: "1.0", id: "sales_overview", title: "Sales overview",
      purpose: "Review historical sales trends and regional totals.",
      recommended_relationship_ids: [],
      visuals: [
        visual("sales_trend", "trend", "Sales over time", "lineChart",
          { category_column_id: "order_date" }, { x: 0, y: 2, width: 6, height: 5 }),
        visual("sales_region", "category", "Sales by region", "clusteredBarChart",
          { category_column_id: "region" }, { x: 6, y: 2, width: 6, height: 5 }),
        visual("sales_total", "kpi", "Total Sales", "cardVisual",
          {}, { x: 0, y: 0, width: 4, height: 2 }),
      ],
    }],
    previews: [
      { visual_id: "sales_trend", rows: historicalRows },
      { visual_id: "sales_region", rows: [{ group_1: "East", value: 490 }, { group_1: "West", value: 560 }] },
      { visual_id: "sales_total", rows: [{ value: 1050 }] },
    ].map(preview => ({
      ...preview, vega_lite_spec: {}, empty_reason: null,
      query_plan_hash: "d".repeat(64), query_result_hash: "e".repeat(64), query_elapsed_ms: 1,
    })),
    planning_provenance: { narrative: "deterministic_compiler", report_theme: "default" },
    status: "approved", updated_at: timestamp, base_hash: "f".repeat(64),
  };
  const health = {
    status: "ready", version: "fixture", state_root: "in-memory browser fixture",
    model_available: true, model_name: "mocked local model", available_memory_gb: 8,
    total_memory_gb: 16, powerbi_desktop_available: false, offline: true,
  };
  const messages = [
    { id: 1, role: "user", content: "What did we discuss earlier?", citations: [], created_at: "2025-07-01T12:01:00Z" },
    { id: 2, role: "assistant", content: "Earlier saved answer.", citations: [], created_at: "2025-07-01T12:01:01Z" },
  ];
  return { workspace, snapshot, draft, health, timestamp, messages };
}
