import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/daxBreakdown.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { breakdownRows, breakdownChartRows } = loaded.exports;
const result = rows => ({ breakdown: {column: "Group", value_column: "Value", field: {name:"Customer Name", data_type:"string"}}, rows });

test("inspection keys retain blank, false, zero and leading-zero strings", () => {
  const values = [null, "", false, 0, "00123", "O'Brien", "(Blank)"];
  const rows = breakdownRows(result(values.map((Group, i) => ({Group, Value: i-2}))));
  assert.deepEqual(rows.map(row => JSON.parse(row.key)), values);
  assert.equal(new Set(rows.map(row => row.key)).size, values.length);
  assert.equal(rows[0].label, "(Blank)");
  assert.equal(rows[4].label, "00123");
  assert.equal(rows[2].value, 0);
  const chart = breakdownChartRows(result(values.map(Group => ({Group, Value: 1}))));
  assert.equal(new Set(chart.map(row => row.label)).size, values.length);
});

test("category charts stay bounded while tables retain all returned groups", () => {
  const data = result(Array.from({length: 100}, (_, i) => ({Group:`Customer ${i}`, Value:100-i})));
  assert.equal(breakdownRows(data).length, 100);
  assert.equal(breakdownChartRows(data).length, 20);
  assert.equal(breakdownChartRows(data)[0].raw, "Customer 0");
});

test("date breakdowns retain years, missing values and the full bounded timeline", () => {
  const data = {...result([{Group:"2024-01-01T00:00:00", Value:2}, {Group:"2025-01-01T00:00:00", Value:null}, {Group:null, Value:8}]), breakdown:{column:"Group",value_column:"Value",grain:"month"}};
  assert.deepEqual(breakdownRows(data).map(row => row.label), ["Jan 2024", "Jan 2025", "(Blank)"]);
  assert.equal(breakdownChartRows(data).length, 2);
  assert.equal(breakdownChartRows(data)[1].value, null);
});
