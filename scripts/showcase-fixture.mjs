// Deterministic, entirely synthetic retail data for UI screenshots.
// No AI inference, Power BI operation, account, source file, or user workspace is used.
export function showcaseFixture() {
  const timestamp = "2025-07-01T12:00:00Z";
  const products = [
    { product_id: "P100", product_name: "Wireless Keyboard", category: "Accessories", unit_price: 79 },
    { product_id: "P200", product_name: "USB-C Dock", category: "Connectivity", unit_price: 129 },
    { product_id: "P300", product_name: "27-inch Display", category: "Displays", unit_price: 249 },
  ];
  const regions = ["East", "West", "Central", "South"].map((region_name, i) => ({ region_id: `R${i + 1}`, region_name }));
  const dates = Array.from({ length: 6 }, (_, i) => ({ date: `2025-${String(i + 1).padStart(2, "0")}-01`, month: ["January", "February", "March", "April", "May", "June"][i], month_number: i + 1 }));
  const orders = dates.flatMap((date, month) => regions.flatMap((region, regionIndex) => products.map((product, productIndex) => {
    const quantity = 4 + month * 2 + regionIndex + productIndex * 2;
    const sales = quantity * product.unit_price;
    return { order_id: `ORD-${String(month * 12 + regionIndex * 3 + productIndex + 1).padStart(4, "0")}`, order_date: date.date,
      product_id: product.product_id, region_id: region.region_id, quantity, sales, profit: Math.round(sales * [0.22, 0.31, 0.26][productIndex] * 100) / 100 };
  })));
  const records = { orders, products, regions, dates };
  const fields = {
    orders: [["order_id", "Order ID", "string"], ["order_date", "Order Date", "date"], ["product_id", "Product ID", "string"], ["region_id", "Region ID", "string"], ["quantity", "Quantity", "integer"], ["sales", "Sales", "number"], ["profit", "Profit", "number"]],
    products: [["product_id", "Product ID", "string"], ["product_name", "Product", "string"], ["category", "Category", "string"], ["unit_price", "Unit Price", "number"]],
    regions: [["region_id", "Region ID", "string"], ["region_name", "Region", "string"]],
    dates: [["date", "Date", "date"], ["month", "Month", "string"], ["month_number", "Month Number", "integer"]],
  };
  const names = { orders: "Orders", products: "Products", regions: "Regions", dates: "Calendar" };
  const workspace = { id: "showcase-workspace", name: "Retail demo · synthetic", kind: "new", active_snapshot_id: "showcase-snapshot", active_draft_id: "showcase-draft", chat_boundaries: [], base_hash: "a".repeat(64) };
  const tables = Object.entries(records).map(([id, rows]) => ({ id: `snapshot_${id}`, original_sha256: "b".repeat(64), normalized_sha256: "b".repeat(64), table: {
    id, name: names[id], row_count: rows.length, columns: fields[id].map(([columnId, name, data_type], index) => {
      const values = [...new Set(rows.map(row => row[columnId]))];
      return { id: columnId, name, data_type, row_count: rows.length, null_count: 0, distinct_count: values.length, distinct_count_exact: true,
        candidate_key: index === 0 && values.length === rows.length, sample_values: values.slice(0, 6) };
    }),
  } }));
  const snapshot = { id: workspace.active_snapshot_id, workspace_id: workspace.id, created_at: timestamp, original_bytes: 9216, normalized_bytes: 6144,
    findings: [{ id: "showcase_dates", table_id: "snapshot_orders", column_id: "order_date", severity: "info", code: "date_range", message: "Synthetic orders cover January through June 2025; all join keys are complete." }], tables, base_hash: "c".repeat(64) };
  const relationships = [["products", "product_id", "product_id"], ["regions", "region_id", "region_id"], ["dates", "order_date", "date"]].map(([toTable, fromColumn, toColumn]) => ({
    id: `orders_${toTable}`, from_table_id: "orders", from_column_id: fromColumn, to_table_id: toTable, to_column_id: toColumn,
    cardinality: "many_to_one", cross_filter_direction: "single", is_active: true, rely_on_referential_integrity: false,
    confidence: 1, review_status: "approved", recommended_by_model: false, blockers: [],
    evidence: ["Synthetic fixture: every fact key exists in the dimension.", "Dimension keys are unique and complete.", "Single-direction dimension-to-fact filter flow."],
  }));
  const measure = (id, name, operation, column, format_string = "#,0.00") => ({ tier: "governed", id, name, operation, table_id: "orders", ...(column ? { column_id: column } : {}), period: "month", format_string });
  const measures = [measure("total_sales", "Total Sales", "sum", "sales"), measure("total_profit", "Total Profit", "sum", "profit"), measure("order_count", "Order Count", "distinct_count", "order_id", "#,0"),
    { ...measure("profit_margin", "Profit Margin", "ratio", undefined, "0.0%"), numerator_measure_id: "total_profit", denominator_measure_id: "total_sales" }];
  const sum = (rows, key) => Math.round(rows.reduce((total, row) => total + row[key], 0) * 100) / 100;
  const totalSales = sum(orders, "sales"), totalProfit = sum(orders, "profit");
  const monthly = dates.map(date => ({ group_1: date.date, value: sum(orders.filter(row => row.order_date === date.date), "sales") }));
  const regional = regions.map(region => ({ group_1: region.region_name, value: sum(orders.filter(row => row.region_id === region.region_id), "sales") }));
  const visual = (id, title, recipe, measure_id, x, y, width, height, binding = {}) => ({ id, title, recipe, visual_type: recipe === "kpi" ? "cardVisual" : recipe === "trend" ? "lineChart" : "clusteredBarChart",
    table_id: "orders", measure_id, detail_column_ids: [], forecast: null, date_filter: null, x, y, width, height, ...binding });
  const visuals = [visual("sales_kpi", "Total Sales", "kpi", "total_sales", 0, 0, 4, 2), visual("profit_kpi", "Total Profit", "kpi", "total_profit", 4, 0, 4, 2),
    visual("margin_kpi", "Profit Margin", "kpi", "profit_margin", 8, 0, 4, 2),
    visual("sales_trend", "Sales by month", "trend", "total_sales", 0, 2, 7, 10, { category_column_id: "order_date" }),
    visual("sales_region", "Sales by region", "category", "total_sales", 7, 2, 5, 10, { table_id: "regions", category_column_id: "region_name" })];
  const money = value => `$${value.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
  // Prepared currency labels keep the synthetic gallery's units and punctuation explicit.
  const results = { sales_kpi: [{ value: money(totalSales) }], profit_kpi: [{ value: money(totalProfit) }], orders_kpi: [{ value: orders.length }], margin_kpi: [{ value: totalProfit / totalSales }], sales_trend: monthly, sales_region: regional };
  const draft = { id: workspace.active_draft_id, workspace_id: workspace.id, snapshot_id: snapshot.id, prompt: "Review sales, profit and regional performance for the synthetic retail demo.",
    understanding: { summary: "Synthetic retail orders joined to product, region and calendar dimensions.", suggested_questions: ["How did sales change over time?"], caveats: ["Synthetic screenshot fixture; no AI inference or Power BI run."] },
    semantic_model: { approved: true, tables: tables.map(({ table }) => ({ table_id: table.id, role: table.id === "orders" ? "fact" : "dimension", display_name: table.name, hidden_column_ids: [] })), relationships },
    measure_proposal: { measures }, pages: [{ version: "1.0", id: "retail_overview", title: "Retail performance", purpose: "Sales, profit and regional mix · synthetic demo", visuals, recommended_relationship_ids: relationships.map(item => item.id) }],
    previews: visuals.map(item => ({ visual_id: item.id, rows: results[item.id], vega_lite_spec: {}, empty_reason: null, query_plan_hash: "d".repeat(64), query_result_hash: "e".repeat(64), query_elapsed_ms: 1 })),
    planning_provenance: { narrative: "deterministic_compiler", report_theme: "default" }, status: "approved", updated_at: timestamp, base_hash: "f".repeat(64) };
  const messages = [{ id: 1, role: "user", content: "What should I notice in this sales overview?", citations: [], created_at: timestamp },
    { id: 2, role: "assistant", content: `The synthetic snapshot contains **${orders.length} orders** across four regions and three products.\n\n- Total sales: **${money(totalSales)}**\n- Total profit: **${money(totalProfit)}**\n- Profit margin: **${(totalProfit / totalSales * 100).toFixed(1)}%**\n\nSales rise across the six months. Compare the regional mix in the adjacent chart, then review the relationships and measures before building.\n\nThis is a prepared fixture answer for the UI showcase.`, citations: [], created_at: timestamp }];
  const health = { status: "ready", version: "0.1.0", state_root: "Synthetic fixture only", model_available: true, model_name: "Qwen 4B", model_digest: "fixture-only", available_memory_gb: 8, total_memory_gb: 16, powerbi_desktop_available: false, offline: true };
  return { workspace, snapshot, draft, messages, health, records, totalSales, totalProfit, monthly, regional };
}

export function injectDemoNotice(html) {
  const notice = `<div class="showcase-notice" role="note">Synthetic demo · AI replies are examples · No AI inference or Power BI run</div>`;
  const style = `<style>body{padding-top:32px}.app-shell{height:calc(100dvh - 32px)!important}.showcase-notice{position:fixed;inset:0 0 auto;height:32px;display:flex;align-items:center;justify-content:center;z-index:9999;background:#fff3bf;border-bottom:1px solid #e6cb71;color:#493c00;font:12px "Segoe UI",sans-serif}</style>`;
  return html.replace("</head>", `${style}</head>`).replace("<body>", `<body>${notice}`);
}
