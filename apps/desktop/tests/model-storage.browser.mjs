// Actual imported Model workbench with fixture-only storage facts and saved rows.
// All engine requests are mocked. No source query, Desktop operation or edit is sent.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15175/").origin;
const transformed = await (await fetch(`${origin}/src/ImportedReport.tsx`)).text();
const transformedMain = await (await fetch(`${origin}/src/main.tsx`)).text();
const reactPath = transformed.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)?.[1];
const clientPath = transformedMain.match(/from\s+["']([^"']*\/react-dom_client\.js[^"']*)["']/)?.[1];
assert.ok(reactPath && clientPath, "Use the actual frontend's Vite React modules");

const modes = [["Orders", "import"], ["RemoteSales", "direct_query"], ["Products", "dual"], ["RecentOrders", "hybrid"], ["LakeSales", "direct_lake"]];
const storage = {
  known: true, kind: "mixed", default_mode: "import",
  table_counts: Object.fromEntries(modes.map(([, mode]) => [mode, 1])),
  tables: modes.map(([name, mode]) => ({ name, mode, partition_modes: mode === "hybrid" ? ["import", "direct_query"] : [mode], local_evaluation: mode === "import" ? "snapshot" : "power_bi" })),
  detail: "The tables use different observed storage modes. This does not identify every source group or connection.",
};
const facts = {
  status: "current", truncated: [], model: { defaultMode: "import" }, storage,
  tables: modes.map(([name]) => ({ name, description: `${name} fixture description` })),
  columns: modes.map(([table]) => ({ table, name: "ID", kind: "data", dataType: "int64" })),
  measures: [], relationships: [], hierarchies: [], roles: [], calculationGroups: [], fieldParameters: [], partitions: [], expressions: [],
};
const fixture = designerForecastFixture();
const snapshot = { ...fixture.snapshot, tables: ["Orders", "Products", "RecentOrders"].map(name => ({ ...fixture.snapshot.tables[0], table: { ...fixture.snapshot.tables[0].table, name, row_count: 1200 } })) };
const report = {
  id: "storage-fixture-report", workspace_id: "storage-fixture-workspace", name: "Storage modes fixture", format: "PBIX", source_hash: "a".repeat(64),
  pages: [], tables: modes.map(([TableName]) => ({ TableName, ColumnName: "ID", PandasDataType: "int64" })), measures: [], relationships: [], warnings: [], model: facts,
  head: { id: "storage-head-1", hash: "b".repeat(64) }, capabilities: { edit_report: true, edit_model: true, reasons: [], thin_report: false },
};
const harness = `<!doctype html><html><head><meta charset="utf-8"><title>Model storage fixture</title></head><body><div id="root"></div><script type="module">
import React from ${JSON.stringify(reactPath)};
import ReactDOMClient from ${JSON.stringify(clientPath)};
import RefreshRuntime from "/@react-refresh";
import "/src/styles.css";
import "/src/studio-design.css";
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {};
window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const { ImportedReportWorkspace } = await import("/src/ImportedReport.tsx");
const initialReport = ${JSON.stringify(report)}, initialSnapshot = ${JSON.stringify(snapshot)};
window.fixture = { callbacks: 0 };
function Harness() {
  const [report, setReport] = React.useState(initialReport);
  const [snapshot, setSnapshot] = React.useState(initialSnapshot);
  window.fixture.setReport = setReport;
  window.fixture.setSnapshot = setSnapshot;
  return React.createElement("main", { className: "fixture-model-shell" }, React.createElement(ImportedReportWorkspace, {
    report, snapshot, view: "model", busy: false,
    onOpenDesktop: async () => { throw new Error("Desktop operations are forbidden in this fixture"); },
    onDax: () => { throw new Error("DAX handoff is not exercised here"); },
    onReportChanged: () => { window.fixture.callbacks++; },
  }));
}
ReactDOMClient.createRoot(document.getElementById("root")).render(React.createElement(Harness));
</script><style>html,body,#root{height:100%;margin:0}.fixture-model-shell{height:100%;padding:24px;background:#f5f5f7}.fixture-model-shell>.imported-report-workspace{height:100%;background:white;border:1px solid #ddd;border-radius:8px}</style></body></html>`;

const evidenceDir = process.env.STUDIO_TEST_EVIDENCE_DIR || join(tmpdir(), "modelcraft-model-storage-audit");
await mkdir(evidenceDir, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  for (const [width, height] of [[1440, 900], [1280, 884]]) {
    const page = await browser.newPage({ viewport: { width, height }, serviceWorkers: "block" });
    await page.context().grantPermissions(["local-network-access"], { origin });
    page.setDefaultTimeout(15000);
    const errors = [], requests = [];
    let currentFacts = structuredClone(facts);
    page.on("pageerror", error => errors.push(String(error)));
    page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
    await page.route(`${origin}/__model-storage-fixture`, route => route.fulfill({ contentType: "text/html", body: harness }));
    await page.route("**/v1/**", async route => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, OPTIONS" };
      if (request.method() === "OPTIONS") return route.fulfill({ status: 204, headers });
      requests.push({ method: request.method(), path });
      if (request.method() === "GET" && path === "/v1/workspaces/storage-fixture-workspace/imported-report/model") return route.fulfill({ json: currentFacts, headers });
      errors.push(`Unexpected engine operation blocked: ${request.method()} ${path}`);
      return route.fulfill({ status: 500, headers, json: { detail: "Unexpected model storage fixture request." } });
    });
    await page.goto(`${origin}/__model-storage-fixture`);
    const summary = page.locator(".model-storage-summary");
    await summary.locator("summary").first().waitFor();
    assert.equal(await summary.getAttribute("open"), null, "Summary starts collapsed so the editor remains primary");
    assert.match(await summary.innerText(), /Model storage.*Mixed storage/s);
    await page.waitForFunction(() => document.querySelector('.me-list [aria-current="true"]'));
    assert.deepEqual(requests.map(request => request.method), ["GET"], "Reading storage never queries rows or updates the model");
    const tables = page.getByRole("navigation", { name: "Tables", exact: true });
    for (const [name, mode] of modes) {
      const row = tables.getByRole("button", { name: new RegExp(`^${name}`) });
      assert.equal(await row.locator(".storage-mode-badge").innerText(), { import: "Import", direct_query: "DirectQuery", dual: "Dual", hybrid: "Hybrid", direct_lake: "Direct Lake" }[mode]);
    }
    await summary.locator(":scope > summary").click();
    assert.match(await summary.innerText(), /3 tables have saved local rows.*do not prove complete model coverage/s);
    const hybridRow = summary.getByRole("row").filter({ hasText: "RecentOrders" });
    assert.match(await hybridRow.innerText(), /Hybrid.*Import portion only.*Power BI native model/s);
    const dualRow = summary.getByRole("row").filter({ hasText: "Products" });
    assert.match(await dualRow.innerText(), /Dual.*Available snapshot.*Power BI native model/s);
    await summary.getByText("What the storage modes mean", { exact: true }).click();
    assert.match(await summary.innerText(), /does not identify every source group or connection/);
    assert.match(await summary.innerText(), /may use a remote or live semantic model/);
    assert.equal(await summary.getByRole("button").count(), 0, "Receipt does not offer mode conversion or connection actions");
    await summary.getByText("What the storage modes mean", { exact: true }).click();
    await tables.getByRole("button", { name: /^RecentOrders/ }).click();
    const note = page.locator(".table-storage-note");
    assert.match(await note.innerText(), /Hybrid.*use the native model for the whole table.*1,200 rows.*only the Import portion/s);
    const queue = page.getByRole("button", { name: "Queue this change", exact: true });
    assert.equal(await queue.isDisabled(), true);
    await page.getByRole("textbox", { name: "Description", exact: true }).fill("Unsaved fixture description");
    assert.equal(await queue.isDisabled(), false, "Existing table properties remain editable");
    await page.getByRole("button", { name: "Reset", exact: true }).click();
    assert.equal(await queue.isDisabled(), true);
    await page.screenshot({ path: join(evidenceDir, `model-storage-mixed-${width}.png`) });
    await tables.getByRole("button", { name: /^Products/ }).click();
    assert.match(await note.innerText(), /Dual.*native model.*Cached rows do not reproduce Dual query behavior/s);
    const layout = await page.evaluate(() => ({ width: innerWidth, body: document.documentElement.scrollWidth, summary: document.querySelector('.model-storage-summary').getBoundingClientRect().right, detail: document.querySelector('.me-detail').getBoundingClientRect().right }));
    assert.ok(layout.body <= layout.width && layout.summary <= layout.width && layout.detail <= layout.width, "No page-level horizontal clipping at a desktop window size");

    currentFacts = { ...structuredClone(facts), storage: { ...structuredClone(storage), known: false, kind: "unknown", detail: "Some tables have unknown or unread storage modes. The summary does not infer their modes from cached rows.", table_counts: { import: 1, unknown: 1 }, tables: [{ ...storage.tables[0] }, { name: "RemoteSales", mode: "unknown", partition_modes: ["unknown"], local_evaluation: "unknown" }] } };
    await page.evaluate(next => window.fixture.setReport(next), { ...report, head: { ...report.head, id: "storage-head-partial" }, model: currentFacts });
    await page.waitForFunction(() => document.querySelector('.model-storage-summary > summary .storage-mode-badge')?.textContent === 'Unknown');
    assert.equal(await tables.getByRole("button", { name: /^Orders/ }).locator(".storage-mode-badge").innerText(), "Import", "Confirmed individual modes survive an unknown aggregate");
    assert.equal(await tables.getByRole("button", { name: /^RemoteSales/ }).locator(".storage-mode-badge").innerText(), "Unknown");
    await page.screenshot({ path: join(evidenceDir, `model-storage-partial-${width}.png`) });

    await page.evaluate(next => window.fixture.setReport(next), { ...report, head: { ...report.head, id: "storage-head-reading" }, model: { ...facts, status: "reading" } });
    await page.getByText("Studio is reading the model. Storage modes are not confirmed yet.", { exact: true }).waitFor();
    assert.equal(await summary.getByRole("row").count(), 0, "A reading receipt does not show stale confirmed modes");
    const noModel = { status: "none", storage: { known: true, kind: "no_local_model", default_mode: null, table_counts: {}, tables: [], detail: "This report has no local model. Its connection may be remote or live; confirm it in Power BI Desktop." } };
    await page.evaluate(next => { window.fixture.setSnapshot(undefined); window.fixture.setReport(next); }, { ...report, head: { ...report.head, id: "storage-head-none" }, model: noModel });
    await page.getByText("This report has no local model to edit here. Check its connection in Power BI Desktop.", { exact: true }).waitFor();
    assert.equal(await summary.locator(":scope > summary .storage-mode-badge").innerText(), "No local model");
    assert.match(await summary.innerText(), /may be remote or live.*confirm it/s);
    assert.match(await summary.innerText(), /No saved local rows are available/);
    assert.equal(await page.getByRole("button", { name: /^Tables and columns/ }).isDisabled(), true);
    assert.ok(requests.every(request => request.method === "GET" && request.path.endsWith("/model")), "All operations stayed read-only metadata requests");
    assert.deepEqual(errors, [], `No browser errors at ${width}px`);
    await page.close();
  }
  console.log("Model storage browser regression passed at 1440 and 1280px: mixed/partial/reading/no-local-model, cached Dual/Hybrid clarity and unsent editor reset.");
} finally { await browser.close(); }
