import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const compiled = await build({ stdin: { contents: 'export * from "./modelStorage"; export * from "./ModelStorageSummary"; export { readModelFacts } from "./modelEditing";', resolveDir: fileURLToPath(new URL("../src/", import.meta.url)), loader: "ts" }, bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" }, logLevel: "silent" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { readModelStorage, tableStorage, ModelStorageSummary, TableStorageNote, readModelFacts } = loaded.exports;
const mixed = {
  known: true, kind: "mixed", default_mode: "import", table_counts: { import: 1, direct_query: 1, dual: 1, hybrid: 1, direct_lake: 1, unknown: 0 },
  tables: [
    { name: "Orders", mode: "import", partition_modes: ["import"], local_evaluation: "snapshot" },
    { name: "Inventory", mode: "direct_query", partition_modes: ["direct_query"], local_evaluation: "power_bi" },
    { name: "Products", mode: "dual", partition_modes: ["dual"], local_evaluation: "power_bi" },
    { name: "RecentOrders", mode: "hybrid", partition_modes: ["import", "direct_query"], local_evaluation: "power_bi" },
    { name: "LakeOrders", mode: "direct_lake", partition_modes: ["direct_lake"], local_evaluation: "power_bi" },
  ], detail: "Observed storage modes; source groups are not inspected.",
};
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));

test("storage remains additive to model facts and never inferred from a model default or cached rows", () => {
  assert.equal(readModelFacts({ status: "current", model: { defaultMode: "import" }, tables: [{ name: "Cached" }], columns: [] }).storage.kind, "unknown");
  const facts = readModelFacts({ status: "current", tables: [], columns: [], storage: mixed });
  assert.equal(facts.storage.kind, "mixed");
  assert.equal(tableStorage(facts.storage, "products").mode, "dual");
  assert.equal(tableStorage(facts.storage, "Products").local_evaluation, "power_bi");
});

test("partial identification keeps confirmed per-table modes while the whole-model summary remains unknown", () => {
  const value = readModelStorage({ ...mixed, known: false, kind: "unknown", table_counts: { import: 1, unknown: 1 }, tables: [mixed.tables[0], { name: "NotRead", mode: "unknown", partition_modes: [], local_evaluation: "unknown" }] });
  assert.equal(value.known, false);
  assert.equal(value.kind, "unknown");
  assert.equal(tableStorage(value, "Orders").mode, "import");
  assert.equal(tableStorage(value, "NotRead").mode, "unknown");
  assert.equal(readModelStorage(mixed, "reading").tables.length, 0);
  assert.equal(readModelStorage(mixed, "unknown").kind, "unknown");
});

test("unsupported modes and contradictory Dual snapshot claims cannot become local Import eligibility", () => {
  const value = readModelStorage({ ...mixed, tables: [{ name: "Remote", mode: "dual", partition_modes: ["dual"], local_evaluation: "snapshot", connection: "private server", password: "secret" }, { name: "NewType", mode: "unsupported", partition_modes: ["private query text"], local_evaluation: "snapshot" }], table_counts: { import: -1, dual: "2", unsupported: 3, unknown: 1 } });
  assert.equal(value.tables[0].local_evaluation, "unknown");
  assert.equal(value.tables[1].mode, "unknown");
  assert.deepEqual(value.table_counts, { unknown: 1 });
  assert.ok(!JSON.stringify(value).includes("private") && !JSON.stringify(value).includes("secret"));
});

test("read-only mixed-mode summary separates partial cached rows and includes precise Dual and Hybrid guidance", () => {
  const markup = render(ModelStorageSummary, { storage: readModelStorage(mixed), cachedTables: ["Orders", "Products", "RecentOrders"] });
  assert.match(markup, /Mixed storage/);
  assert.match(markup, /3 tables have saved local rows/);
  assert.match(markup, /do not prove complete model coverage/);
  assert.match(markup, /Import portion only/);
  assert.match(markup, /Power BI can use cached rows or query the source/);
  assert.match(markup, /does not identify every source group/);
  assert.doesNotMatch(markup, /<select|<input|<button|\bopen=""/);
  const hybrid = render(TableStorageNote, { table: mixed.tables[3], cachedRows: 25 });
  assert.match(hybrid, /25 rows in the local snapshot/);
  assert.match(hybrid, /only the Import portion/);
});

test("absence of a local model does not assert a verified live connection", () => {
  const storage = readModelStorage({ known: true, kind: "no_local_model", default_mode: "unknown", table_counts: {}, tables: [], detail: "No embedded local model." }, "none");
  const markup = render(ModelStorageSummary, { storage });
  assert.match(markup, /No local model/);
  assert.match(markup, /may use a remote or live semantic model/);
  assert.match(markup, /No saved local rows/);
});
