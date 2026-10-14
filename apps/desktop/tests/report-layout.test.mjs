import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("../src/reportLayout.ts", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { clampPlacement, canPlace, arrangeVisuals, insertVisual, overlaps } = loaded.exports;
const v = (id, recipe, x, y, width, height) => ({ id, recipe, x, y, width, height, title: id, table_id: "orders", measure_id: "sales", category_column_id: "region", detail_column_ids: [] });
const fullOverview = [v("sales", "kpi", 0, 0, 4, 2), v("profit", "kpi", 4, 0, 4, 2), v("quantity", "kpi", 8, 0, 4, 2), v("trend", "trend", 0, 2, 8, 10), v("region", "category", 8, 2, 4, 10)];
function valid(visuals) {
  for (const visual of visuals) assert.equal(canPlace(visuals, visual.id, visual), true, visual.id);
}
test("inserting into a full overview compacts without overlap or losing bindings", () => {
  const before = structuredClone(fullOverview);
  const added = v("segment", "category", 0, 7, 6, 5);
  const result = insertVisual(fullOverview, added);
  assert.ok(result?.rearranged);
  assert.equal(result.visuals.length, 6);
  valid(result.visuals);
  assert.deepEqual(fullOverview, before);
  for (const original of [...before, added]) {
    const actual = result.visuals.find(item => item.id === original.id);
    for (const key of ["id", "recipe", "title", "table_id", "measure_id", "category_column_id", "detail_column_ids"]) assert.deepEqual(actual[key], original[key]);
  }
});
test("roomy pages preserve all existing coordinates on insertion", () => {
  const existing = [v("sales", "kpi", 0, 0, 4, 2)];
  const result = insertVisual(existing, v("profit", "kpi", 0, 0, 4, 2));
  assert.equal(result.rearranged, false);
  assert.deepEqual(result.visuals[0], existing[0]);
  valid(result.visuals);
});
test("impossible layouts and the twelve-visual cap fail without partial changes", () => {
  const large = Array.from({ length: 5 }, (_, i) => v(`trend_${i}`, "trend", 0, 0, 6, 5));
  assert.equal(arrangeVisuals(large, true), undefined);
  const cards = Array.from({ length: 12 }, (_, i) => v(`card_${i}`, "kpi", 0, 0, 4, 2));
  valid(arrangeVisuals(cards, true));
  assert.equal(insertVisual(cards, v("extra", "kpi", 0, 0, 4, 2)), undefined);
});
test("resizing and recipe conversion obey compiler minimums and page edges", () => {
  assert.deepEqual(clampPlacement(v("card", "kpi", 8, 10, 4, 2), { width: 8, height: 5 }), { x: 4, y: 7, width: 8, height: 5 });
  assert.deepEqual(clampPlacement(v("trend", "trend", 11, 11, 1, 1)), { x: 6, y: 7, width: 6, height: 5 });
  assert.deepEqual(clampPlacement(v("table", "detail", 0, 0, 8, 6), { x: NaN, width: Infinity, y: -4, height: 2.5 }), { x: 0, y: 0, width: 8, height: 6 });
});
test("touching edges are allowed but overlap and fractional grid positions are rejected", () => {
  const left = v("left", "category", 0, 0, 4, 5);
  const right = v("right", "category", 4, 0, 4, 5);
  assert.equal(overlaps(left, right), false);
  assert.equal(canPlace([left], "right", { ...right, x: 3 }), false);
  assert.equal(canPlace([], "right", { x: 4.5, y: 0, width: 4, height: 5 }), false);
});
test("arranging preserves size and binding while restoring a deterministic reading order", () => {
  const input = [v("table", "detail", 0, 0, 8, 6), v("card", "kpi", 0, 0, 4, 2)];
  const result = arrangeVisuals(input);
  valid(result);
  assert.deepEqual(result.map(item => [item.id, item.width, item.height]), input.map(item => [item.id, item.width, item.height]));
  assert.deepEqual(arrangeVisuals(input), result);
});
