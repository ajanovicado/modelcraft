import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/daxDatePreview.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { periodLabel, dateChartRows } = loaded.exports;

test("date labels preserve calendar dates and distinguish years", () => {
  assert.equal(periodLabel("2024-01-01T00:00:00", "day"), "Jan 1, 2024");
  assert.equal(periodLabel("2024-01-01", "month"), "Jan 2024");
  assert.equal(periodLabel("2025-01-01", "month"), "Jan 2025");
  assert.equal(periodLabel("2024-04-01", "quarter"), "Q2 2024");
  assert.equal(periodLabel("2024-01-01", "year"), "2024");
});

test("date charts retain every bounded period, zeroes, negatives and blank measures", () => {
  const rows = Array.from({length: 100}, (_, i) => ({ Period: `${1900+i}-01-01`, Result: i === 0 ? -2 : i === 1 ? 0 : i === 2 ? null : i }));
  const chart = dateChartRows({ date_preview: { column: "Period", value_column: "Result", grain: "year" }, rows: [...rows, { Period: null, Result: 45 }] });
  assert.equal(chart.length, 100);
  assert.deepEqual(chart[0], { period: "1900-01-01", label: "1900", value: -2 });
  assert.equal(chart[1].value, 0);
  assert.equal(chart[2].value, null);
  assert.equal(chart[99].period, "1999-01-01");
});
