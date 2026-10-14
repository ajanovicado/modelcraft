import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const compiled = await build({ entryPoints: [new URL("../src/DesignerVisualPreview.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { DesignerVisualPreview, samePreviewBinding, designerChartSpec, formatPreviewValue } = loaded.exports;
const visual = { id: "sales", recipe: "kpi", title: "Sales", visual_type: "cardVisual", table_id: "orders", measure_id: "total_sales", detail_column_ids: [], x: 0, y: 0, width: 4, height: 2 };
const measure = { id: "total_sales", name: "Total Sales", format_string: "#,#0.00", tier: "governed" };
const preview = { visual_id: "sales", rows: [{ value: 7800 }], vega_lite_spec: {} };
const render = (overrides = {}) => renderToStaticMarkup(React.createElement(DesignerVisualPreview, { visual, saved: visual, preview, measure, category: "Region", ...overrides }));

test("draft design review shows existing provenance without a theme preset picker", async () => {
  const summary = await build({ entryPoints: [new URL("../src/DesignSummary.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
  const module = { exports: {} };
  new Function("require", "module", "exports", summary.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
  const html = renderToStaticMarkup(React.createElement(module.exports.DesignSummary, { draft: { planning_provenance: { layout_template: "executive_overview", layout_template_source: "local_model", report_theme: "calm_teal", report_theme_source: "user" } } }));
  assert.match(html, /Executive overview layout/);
  assert.match(html, /Calm teal theme/);
  assert.match(html, /Chosen by Local AI/);
  assert.doesNotMatch(html, /<button|radiogroup|role="radio"/);
});

test("KPI uses actual snapshot value and retains its exact tooltip", () => {
  assert.match(render(), /7,800/);
  assert.match(render(), /Exact value: 7800/);
});
test("binding changes suppress stale numbers until revalidation", () => {
  for (const update of [{ measure_id: "profit" }, { table_id: "other" }, { category_column_id: "segment" }, { recipe: "detail" }, { detail_column_ids: ["sales"] }]) {
    const html = render({ visual: { ...visual, ...update } });
    assert.match(html, /Preview needs refresh/);
    assert.doesNotMatch(html, /7,800/);
  }
});
test("layout, title and style edits retain the same evidence", () => {
  assert.equal(samePreviewBinding({ ...visual, title: "Revenue", x: 3, width: 2, visual_type: "new-style" }, visual), true);
});

test("date changes and clearing a filter invalidate saved preview evidence", () => {
  const filtered = { ...visual, date_filter: { table_id: "orders", column_id: "order_date", start: "2025-02-01", end: "2025-02-28" } };
  assert.equal(samePreviewBinding(filtered, visual), false);
  assert.equal(samePreviewBinding(visual, filtered), false);
  assert.equal(samePreviewBinding({ ...filtered, date_filter: { ...filtered.date_filter, end: "2025-03-01" } }, filtered), false);
  assert.equal(samePreviewBinding({ ...visual, date_filter: null }, visual), true);
  assert.doesNotMatch(render({ visual: filtered }), /7,800/);
});
test("new visuals never borrow another visual's data", () => {
  assert.match(render({ saved: undefined }), /Preview needs refresh/);
  assert.doesNotMatch(render({ saved: undefined }), /7,800/);
});
test("show data exposes KPI evidence as an accessible table without bypassing freshness", () => {
  const html = render({ view: "table" });
  assert.match(html, /<table aria-label="Sales snapshot values"/);
  assert.match(html, /7,800/);
  assert.match(html, /Total Sales/);
  assert.doesNotMatch(render({ view: "table", saved: undefined }), /7,800/);
});
test("expanded data views remain paged and bounded", () => {
  const detail = { ...visual, recipe: "detail", detail_column_ids: ["order"] };
  const html = render({ visual: detail, saved: detail, view: "table", rowOffset: 50, rowLimit: 10000, preview: { ...preview, rows: Array.from({ length: 140 }, (_, i) => ({ Order: `QA-${i}` })) } });
  assert.match(html, /QA-50/);
  assert.match(html, /QA-99/);
  assert.doesNotMatch(html, /QA-100/);
  assert.match(html, /Showing 50 of 140 preview rows/);
});
test("missing and empty evidence does not fabricate zero", () => {
  assert.match(render({ preview: undefined }), /No preview data/);
  assert.match(render({ preview: { ...preview, rows: [] } }), /Could not compute preview/);
  assert.equal(formatPreviewValue(null), "—");
  assert.equal(formatPreviewValue(0), "0");
});
test("empty preview states name the actual gap", () => {
  const { previewEmptyCopy } = loaded.exports;
  assert.match(previewEmptyCopy(visual, undefined, undefined).title, /Missing binding/);
  assert.match(previewEmptyCopy(visual, { visual_id: "sales", rows: [], empty_reason: "advanced_dax" }, { ...measure, tier: "advanced_dax" }).title, /Calculated in Power BI/);
  assert.match(previewEmptyCopy(visual, { visual_id: "sales", rows: [], empty_reason: "pending_relationship" }, measure).title, /Relationship not approved/);
  // A lab-saved formula that previews locally names the real gap, not "cannot evaluate".
  assert.match(previewEmptyCopy(visual, { visual_id: "sales", rows: [], empty_reason: "pending_relationship" }, { ...measure, tier: "advanced_dax" }).title, /Relationship not approved/);
  assert.match(previewEmptyCopy(visual, { visual_id: "sales", rows: [], query_plan_hash: "a".repeat(64), empty_reason: "empty_result" }, measure).title, /Empty query result/);
  assert.match(previewEmptyCopy(visual, { visual_id: "sales", rows: [], empty_reason: "unsupported_operation" }, measure).title, /No local preview/);
});
test("advanced DAX without calculated evidence is deferred honestly", () => {
  assert.match(render({ measure: { ...measure, tier: "advanced_dax" }, preview: { ...preview, rows: [{ value: null }] } }), /Calculated in Power BI/);
});
test("percentage measures keep their unit", () => {
  assert.equal(formatPreviewValue(0.20345, "0.0%"), "20.3%");
  assert.equal(formatPreviewValue(-0.1, "0.00%"), "-10.00%");
});
test("column and bar styles map to the correct axes with exact-value tooltips", () => {
  const column = designerChartSpec({ ...visual, recipe: "category", visual_type: "clusteredColumnChart" }, preview, "Total Sales", "Region");
  assert.equal(column.encoding.x.type, "nominal");
  assert.equal(column.encoding.y.type, "quantitative");
  assert.equal(column.encoding.tooltip[1].field, "value");
  const bar = designerChartSpec({ ...visual, recipe: "category", visual_type: "clusteredBarChart" }, preview, "Total Sales", "Region");
  assert.equal(bar.encoding.y.type, "nominal");
  assert.equal(bar.encoding.x.type, "quantitative");
  assert.equal(bar.data.values, preview.rows);
});
test("trend styles preserve dates and change only the mark", () => {
  const trend = { ...visual, recipe: "trend", visual_type: "lineChart" };
  assert.equal(designerChartSpec(trend, preview, "Sales", "Order Date").mark.type, "line");
  assert.equal(designerChartSpec({ ...trend, visual_type: "areaChart" }, preview, "Sales", "Order Date").mark.type, "area");
  assert.equal(designerChartSpec(trend, preview, "Sales", "Order Date").encoding.x.type, "temporal");
});

test("forecast settings preserve historical evidence and identify where projections are calculated", () => {
  const trend = { ...visual, recipe: "trend", visual_type: "lineChart", forecast: { forecast_length: 6, confidence: 95, ignore_last: 0 } };
  const actuals = { ...preview, rows: [{ group_1: "2025-01-01", value: 100 }] };
  assert.equal(samePreviewBinding(trend, { ...trend, forecast: null }), true);
  assert.equal(designerChartSpec(trend, actuals, "Sales", "Order Date").data.values, actuals.rows);
  assert.equal(designerChartSpec(trend, actuals, "Sales", "Order Date").layer, undefined);
  assert.match(render({ visual: trend, saved: trend, preview: actuals }), /Forecast enabled · calculated in Power BI/);
  assert.match(render({ visual: trend, saved: trend, preview: undefined }), /Forecast enabled · calculated in Power BI/);
  assert.match(render({ visual: trend, saved: trend, preview: undefined }), /No preview data/);
  assert.match(render({ visual: trend, saved: trend, preview: actuals, view: "table" }), /2025-01-01/);
  assert.match(render({ visual: trend, saved: trend, preview: actuals, view: "table" }), /100/);
});
test("unqualified render styles show aggregate values, not a fake chart", () => {
  const special = { ...visual, recipe: "category", visual_type: "waterfallChart", category_column_id: "region" };
  const html = render({ visual: special, saved: special, preview: { ...preview, rows: [{ group_1: "East", value: 1500 }] } });
  assert.match(html, /East/);
  assert.match(html, /1,500/);
  assert.match(html, /final shape in Power BI/);
});
test("detail tables expose a bounded sample with explicit row count", () => {
  const detail = { ...visual, recipe: "detail", detail_column_ids: ["order"] };
  const html = render({ visual: detail, saved: detail, preview: { ...preview, rows: Array.from({ length: 20 }, (_, i) => ({ Order: `QA-${i}` })) } });
  assert.match(html, /Showing 8 of 20 preview rows/);
  assert.doesNotMatch(html, /QA-8/);
});
