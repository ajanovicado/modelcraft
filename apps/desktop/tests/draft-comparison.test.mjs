import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const compiled = await build({
  entryPoints: [fileURLToPath(new URL("../src/draftComparison.ts", import.meta.url))],
  bundle: true, platform: "node", format: "cjs", packages: "external", write: false,
});
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { compareDrafts } = loaded.exports;
const draft = () => designerForecastFixture().draft;
const noChanges = {
  added_measures: [], changed_measures: [], removed_measures: [],
  added_pages: [], changed_pages: [], removed_pages: [],
};
const changes = review => Object.fromEntries(Object.keys(noChanges).map(key => [key, review[key]]));

test("unchanged draft keeps empty deltas and accurate summary despite new receipt metadata", () => {
  const before = draft(), after = structuredClone(before);
  after.base_hash = "1".repeat(64);
  after.updated_at = "2025-07-02T12:00:00Z";
  const review = compareDrafts(before, after, "Review this draft");
  assert.deepEqual(changes(review), noChanges);
  assert.deepEqual(review.before, { measures: 1, pages: 1, visuals: 3 });
  assert.deepEqual(review.after, review.before);
  assert.equal(review.prompt, "Review this draft");
  assert.equal(review.approval_reset, false);
});

test("page and measure renames are changes to stable objects rather than additions/removals", () => {
  const before = draft(), after = structuredClone(before);
  after.pages[0].title = "Sales performance";
  after.measure_proposal.measures[0].name = "Reviewed Sales";
  const review = compareDrafts(before, after, "Rename this page and measure");
  assert.deepEqual(changes(review), {
    ...noChanges, changed_pages: ["Sales performance"], changed_measures: ["Reviewed Sales"],
  });
  assert.deepEqual(review.before, review.after);
});

test("replacing object IDs is an addition and removal even when display labels are identical", () => {
  const before = draft(), after = structuredClone(before);
  after.pages[0].id = "replacement_overview";
  after.measure_proposal.measures[0].id = "replacement_sales";
  assert.deepEqual(changes(compareDrafts(before, after, "Replace these objects")), {
    ...noChanges,
    added_pages: ["Sales overview"], removed_pages: ["Sales overview"],
    added_measures: ["Total Sales"], removed_measures: ["Total Sales"],
  });
});

test("actual additions/removals retain their labels and measure/page/visual count deltas", () => {
  const before = draft(), after = structuredClone(before);
  after.pages = [
    { ...structuredClone(before.pages[0]), id: "regional_review", title: "Regional review",
      visuals: [structuredClone(before.pages[0].visuals[1])] },
    { ...structuredClone(before.pages[0]), id: "sales_detail", title: "Sales detail",
      visuals: [structuredClone(before.pages[0].visuals[2])] },
  ];
  after.measure_proposal.measures = [
    { ...before.measure_proposal.measures[0], id: "sales_average", name: "Average Sales", operation: "average" },
    { ...before.measure_proposal.measures[0], id: "sales_minimum", name: "Minimum Sales", operation: "minimum" },
  ];
  after.pages[0].visuals[0].measure_id = "sales_average";
  after.pages[1].visuals[0].measure_id = "sales_minimum";
  const review = compareDrafts(before, after, "Replace overview");
  assert.deepEqual(changes(review), {
    ...noChanges,
    added_pages: ["Regional review", "Sales detail"], removed_pages: ["Sales overview"],
    added_measures: ["Average Sales", "Minimum Sales"], removed_measures: ["Total Sales"],
  });
  assert.deepEqual(review.before, { measures: 1, pages: 1, visuals: 3 });
  assert.deepEqual(review.after, { measures: 2, pages: 2, visuals: 2 });
});

test("forecast-only updates change their page without changing summary counts or historical evidence", () => {
  const before = draft(), after = structuredClone(before);
  after.pages[0].visuals[0].forecast = { forecast_length: 6, confidence: 95, ignore_last: 0, seasonality: null };
  after.status = "draft";
  after.semantic_model.approved = false;
  const review = compareDrafts(before, after, "Add forecasting to this chart");
  assert.deepEqual(changes(review), { ...noChanges, changed_pages: ["Sales overview"] });
  assert.deepEqual(review.before, review.after);
  assert.deepEqual(after.previews, before.previews);
  assert.equal(review.approval_reset, true);
});

test("a measure formula or visual layout change is still reported as changed", () => {
  const before = draft(), after = structuredClone(before);
  after.measure_proposal.measures[0].operation = "average";
  after.pages[0].visuals[0].y = 3;
  assert.deepEqual(changes(compareDrafts(before, after, "Update measure and layout")), {
    ...noChanges, changed_measures: ["Total Sales"], changed_pages: ["Sales overview"],
  });
});

test("objects with the same label remain independently identifiable", () => {
  const before = draft();
  before.pages.push({ ...structuredClone(before.pages[0]), id: "second_overview",
    visuals: before.pages[0].visuals.map(visual => ({ ...visual, id: `second_${visual.id}` })),
  });
  before.measure_proposal.measures.push({ ...before.measure_proposal.measures[0], id: "second_sales" });
  const after = structuredClone(before);
  after.pages[1].title = "Other sales overview";
  after.measure_proposal.measures[1].name = "Other Sales";
  assert.deepEqual(changes(compareDrafts(before, after, "Rename second objects")), {
    ...noChanges, changed_measures: ["Other Sales"], changed_pages: ["Other sales overview"],
  });
});
