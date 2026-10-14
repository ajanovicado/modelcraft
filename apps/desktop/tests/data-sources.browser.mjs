// Real data-source components; every engine request is fulfilled by a fixture.
// No database, authentication, user workspace or Power BI operation is used.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
fixture.snapshot.tables[0].table.columns.push(
  { ...fixture.snapshot.tables[0].table.columns[2], id: "safe_id", name: "Safe ID", data_type: "integer", sample_values: ["1234567890123456"] },
  { ...fixture.snapshot.tables[0].table.columns[2], id: "large_text_id", name: "Exact large ID", data_type: "string", sample_values: ["123456789012345678"] },
);
const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15175/").origin;
const transformed = await (await fetch(`${origin}/src/DataWorkbench.tsx`)).text();
const transformedMain = await (await fetch(`${origin}/src/main.tsx`)).text();
const reactPath = transformed.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)?.[1];
const clientPath = transformedMain.match(/from\s+["']([^"']*\/react-dom_client\.js[^"']*)["']/)?.[1];
assert.ok(reactPath && clientPath, "Use the same Vite React modules as the actual frontend");
const storedSource = { provider: "sqlserver", profile: "ReviewedSales", schema_name: "sales", tables: ["Orders"], max_rows: 500000 };
const harness = `<!doctype html><html><head><meta charset="utf-8"><title>Data sources regression fixture</title></head><body><div id="root"></div><script type="module">
import React from ${JSON.stringify(reactPath)};
import ReactDOMClient from ${JSON.stringify(clientPath)};
import RefreshRuntime from "/@react-refresh";
import "/src/styles.css";
import "/src/studio-design.css";
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {};
window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const { DataWorkbench } = await import("/src/DataWorkbench.tsx");
const fixture = ${JSON.stringify(fixture)};
const storedSource = ${JSON.stringify(storedSource)};
window.fixture = { refreshed: 0, openedDesktop: 0, importedReport: 0, failRefresh: true };
function Harness() {
  const [workspace, setWorkspace] = React.useState(fixture.workspace);
  const [snapshot, setSnapshot] = React.useState(undefined);
  const [busy, setBusy] = React.useState(false);
  const [canImportReport, setCanImportReport] = React.useState(true);
  window.fixture.setWorkspace = setWorkspace;
  window.fixture.setSnapshot = setSnapshot;
  window.fixture.setCanImportReport = setCanImportReport;
  window.fixture.setBusy = setBusy;
  return React.createElement("main", { className: "fixture-data-shell" }, React.createElement(DataWorkbench, {
    workspace, snapshot, busy, onImportBusy: setBusy,
    onImported: async () => {
      window.fixture.refreshed++;
      if (window.fixture.failRefresh) throw new Error("Fixture snapshot refresh temporarily unavailable.");
      setSnapshot({ ...fixture.snapshot, id: "new-imported-snapshot", database_source: storedSource });
      setWorkspace({ ...fixture.workspace, base_hash: "d".repeat(64) });
    },
    onOpenSourceReport: () => window.fixture.openedDesktop++,
    onImportReport: canImportReport ? () => window.fixture.importedReport++ : undefined,
  }, React.createElement("section", null, React.createElement("h2", null, "Local file selection fixture"), React.createElement("p", null, "Local files are selected separately; no files are read by this fixture."))));
}
ReactDOMClient.createRoot(document.getElementById("root")).render(React.createElement(Harness));
</script><style>html,body,#root{height:100%;margin:0}.fixture-data-shell{height:100%;padding:24px;background:#f5f5f7}.fixture-data-shell>.data-workbench{height:100%;background:white;border:1px solid #ddd;border-radius:8px}</style></body></html>`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
await page.context().grantPermissions(["local-network-access"], { origin });
page.setDefaultTimeout(15000);
const errors = [], requests = [], imports = [];
let driverChecks = 0, browseCalls = 0, sqlReady = false, gatedBrowse;
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => {
  if (message.type() !== "error") return;
  if (/\/v1\/connections\/(drivers|browse|import)$/.test(message.location().url) && /503/.test(message.text())) return;
  errors.push(message.text());
});
await page.route(`${origin}/__data-source-fixture`, route => route.fulfill({ contentType: "text/html", body: harness }));
await page.route("**/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" };
  const json = body => route.fulfill({ json: body, headers });
  if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
  const body = method === "GET" ? undefined : request.postDataJSON();
  requests.push({ path, method, body });
  if (path === "/v1/connections/drivers" && method === "GET") {
    if (++driverChecks === 1) return route.fulfill({ status: 503, headers, json: { detail: "Fixture prerequisites temporarily unavailable." } });
    return json([
      { id: "snowflake", name: "Snowflake", installed: true, ready: true, detail: "Snowflake connector available. A configured profile and sign-in are still required." },
      { id: "sqlserver", name: "SQL Server / Azure SQL", installed: true, ready: sqlReady, detail: sqlReady ? "ODBC Driver 18 available. A configured DSN and sign-in are still required." : "SQL Server ODBC Driver 17 or 18 is required. The Python connector alone is insufficient." },
    ]);
  }
  if (path === "/v1/connections/browse" && method === "POST") {
    browseCalls++;
    if (browseCalls === 2) return route.fulfill({ status: 503, headers, json: { detail: "Fixture sign-in expired. Reconnect to browse tables." } });
    if (body.source.profile === "DelayedDSN") {
      gatedBrowse = route;
      return;
    }
    return json({ tables: [{ name: "Orders", kind: "BASE TABLE" }], has_more: true });
  }
  if (path === "/v1/connections/preview" && method === "POST") return json({ columns: ["Order ID", "Sales"], rows: [["1", "120"]] });
  if (path === "/v1/connections/import" && method === "POST") {
    imports.push(body);
    if (imports.length === 1) return route.fulfill({ status: 503, headers, json: { detail: "Fixture import temporarily unavailable. Retry the reviewed request." } });
    return json({ ...fixture.snapshot, database_source: storedSource });
  }
  if (/^\/v1\/snapshots\/[^/]+\/preview$/.test(path) && method === "POST") {
    const profile = fixture.snapshot.tables[0].table;
    const columns = body.column_ids.map(id => profile.columns.find(column => column.id === id));
    const values = { order_date: "2025-01-01", region: "East", sales: 120, safe_id: 1234567890123456, large_text_id: "123456789012345678" };
    return json({ columns: columns.map(({ id, name, data_type }) => ({ id, name, data_type })), rows: [columns.map(column => values[column.id])], offset: body.offset, total_rows: 1, snapshot_rows: 1, has_more: false, truncated_cells: false, elapsed_ms: 1 });
  }
  errors.push(`Unmocked request blocked: ${method} ${path}`);
  return route.fulfill({ status: 500, headers, json: { detail: "Unexpected data-source fixture request." } });
});
const capture = async name => {
  if (!process.env.STUDIO_TEST_EVIDENCE_DIR) return;
  await mkdir(process.env.STUDIO_TEST_EVIDENCE_DIR, { recursive: true });
  await page.screenshot({ path: join(process.env.STUDIO_TEST_EVIDENCE_DIR, `${name}.png`), animations: "disabled" });
};
const connection = page.getByRole("region", { name: "Database connections" });
const connect = connection.getByRole("button", { name: "Connect & browse", exact: true });

try {
  await page.goto(`${origin}/__data-source-fixture`, { waitUntil: "networkidle" });
  assert.equal(new URL(page.url()).origin, origin);
  assert.equal(await page.title(), "Data sources regression fixture");
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  await connection.getByRole("button", { name: "Retry connection check", exact: true }).click();
  await connection.getByText(/Snowflake connector available/).waitFor();
  const connectorHelp = page.locator(".powerbi-source-help");
  assert.equal(await connectorHelp.getAttribute("open"), null, "The broader connector help starts collapsed");
  await connectorHelp.locator("summary").click();
  await connectorHelp.getByText(/Set up the source with Get data in Power BI Desktop/).waitFor();
  assert.equal(await connectorHelp.getByRole("link", { name: "View connector catalog", exact: true }).getAttribute("href"), "https://learn.microsoft.com/en-us/power-query/connectors/");
  await connectorHelp.getByRole("button", { name: "Open Power BI report", exact: true }).click();
  assert.equal(await page.evaluate(() => window.fixture.importedReport), 1);
  await page.evaluate(() => window.fixture.setBusy(true));
  await page.waitForFunction(() => document.querySelector(".powerbi-source-help button")?.disabled);
  assert.equal(await connectorHelp.getByRole("button", { name: "Open Power BI report", exact: true }).isDisabled(), true);
  await page.evaluate(() => { window.fixture.setBusy(false); window.fixture.setCanImportReport(false); });
  await connectorHelp.getByRole("button", { name: "Open Power BI report", exact: true }).waitFor({ state: "detached" });
  await page.evaluate(() => { window.fixture.setCanImportReport(true); window.fixture.importedReport = 0; });
  await connectorHelp.getByRole("button", { name: "Open Power BI report", exact: true }).waitFor();
  await capture("powerbi-connector-help-1440");
  await connectorHelp.locator("summary").click();
  await connection.getByRole("textbox", { name: "Connection profile", exact: true }).fill("FixtureSnowflake");
  // Two events in one frame exercise the synchronous guard, before React disables the control.
  await connect.evaluate(button => { button.click(); button.click(); });
  await connection.getByRole("checkbox", { name: /Orders/ }).waitFor();
  assert.equal(browseCalls, 1, "A double click must make one connection request");
  await connection.getByRole("checkbox", { name: /Orders/ }).check();
  await connection.getByRole("button", { name: "Preview", exact: true }).click();
  await connection.getByRole("heading", { name: /Orders · First 50 rows/ }).waitFor();
  await connection.getByRole("textbox", { name: "Find a table", exact: true }).fill("Orders");
  await connection.getByRole("button", { name: "Search", exact: true }).click();
  await connection.getByRole("alert").filter({ hasText: "Fixture sign-in expired" }).waitFor();
  assert.equal(await connection.getByRole("checkbox").count(), 0, "Failed reconnect must remove stale tables and selections");
  assert.equal(await connection.locator(".database-sample").count(), 0);
  assert.equal(await connection.getByRole("button", { name: /Review import/ }).count(), 0);
  await capture("data-source-reconnect-failed-1440");

  await connection.getByRole("combobox", { name: "Source", exact: true }).selectOption("sqlserver");
  assert.equal(await connection.getByRole("textbox", { name: "Schema", exact: true }).inputValue(), "dbo");
  assert.equal(await connection.getByRole("textbox", { name: "ODBC data source (DSN)", exact: true }).inputValue(), "");
  await connection.getByRole("textbox", { name: "ODBC data source (DSN)", exact: true }).fill("DelayedDSN");
  assert.equal(await connect.isDisabled(), true, "Installed Python SDK cannot imply native ODBC driver readiness");
  await connection.getByText(/Python connector alone is insufficient/).waitFor();
  await page.setViewportSize({ width: 1280, height: 884 });
  await capture("data-source-odbc-prerequisite-1280");
  sqlReady = true;
  await connection.getByRole("button", { name: "Check prerequisites", exact: true }).click();
  await connection.getByText(/ODBC Driver 18 available/).waitFor();
  await connect.click();
  while (!gatedBrowse) await new Promise(resolve => setTimeout(resolve, 10));
  await page.evaluate(workspace => window.fixture.setWorkspace({ ...workspace, base_hash: "b".repeat(64) }), fixture.workspace);
  await page.waitForFunction(() => !document.querySelector(".database-sources").textContent.includes("Connecting and listing tables"));
  await gatedBrowse.fulfill({ json: { tables: [{ name: "OldSourceTable", kind: "BASE TABLE" }], has_more: false } }).catch(() => {});
  assert.equal(await connection.getByText("OldSourceTable", { exact: true }).count(), 0, "An old connection result cannot populate a changed workspace head");

  await page.evaluate(({ snapshot, source }) => window.fixture.setSnapshot({ ...snapshot, database_source: source }), { snapshot: fixture.snapshot, source: storedSource });
  const beforeRestore = requests.length;
  await connection.getByRole("button", { name: "Import again", exact: true }).click();
  await connection.getByText(/Saved connection and table selection restored/).waitFor();
  assert.equal(requests.length, beforeRestore, "Opening a saved import review must not authenticate or query");
  assert.equal(await connection.getByRole("textbox", { name: "ODBC data source (DSN)", exact: true }).inputValue(), storedSource.profile);
  assert.equal(await connection.getByRole("textbox", { name: "Schema", exact: true }).inputValue(), storedSource.schema_name);
  assert.equal(await connection.getByRole("combobox", { name: "Maximum rows per table", exact: true }).inputValue(), "500000");
  assert.equal(await connection.getByRole("checkbox", { name: /Orders/ }).isChecked(), true);
  await capture("data-source-explicit-refresh-review-1280");
  await connection.getByRole("button", { name: "Import snapshot", exact: true }).click();
  await connection.getByRole("alert").filter({ hasText: "Fixture import temporarily unavailable" }).waitFor();
  await connection.getByRole("button", { name: "Import snapshot", exact: true }).click();
  await connection.getByRole("button", { name: "Load imported snapshot", exact: true }).waitFor();
  assert.equal(imports.length, 2);
  assert.deepEqual(imports[1], imports[0], "Retry must reuse the exact source, table selection, cap, base hash and idempotency key");
  assert.equal(imports[0].max_rows, 500000);
  assert.equal(imports[0].expected_base_hash, "b".repeat(64));
  assert.equal(await page.evaluate(() => window.fixture.refreshed), 1);
  await page.evaluate(() => { window.fixture.failRefresh = false; });
  await connection.getByRole("button", { name: "Load imported snapshot", exact: true }).click();
  await page.getByRole("region", { name: "Orders table", exact: true }).waitFor();
  await page.getByRole("cell", { name: "123456789012345678", exact: true }).waitFor();
  const safeId = page.locator('.workbench-grid td[title="1234567890123456"]');
  assert.equal((await safeId.innerText()).replace(/\D/g, ""), "1234567890123456", "A safe 16-digit integer must not be rounded for display");
  assert.equal(await page.getByRole("cell", { name: "123456789012345678", exact: true }).innerText(), "123456789012345678", "A large identifier serialized as text must keep every digit");
  assert.equal(imports.length, 2, "Recovery after a successful import must reload, not import again");
  assert.equal(await page.evaluate(() => window.fixture.refreshed), 2);
  await page.getByRole("textbox", { name: "Find columns", exact: true }).fill("Missing field");
  await page.getByText("No matching columns.", { exact: true }).waitFor();
  await page.evaluate(snapshot => window.fixture.setSnapshot({ ...snapshot, id: "another-data-snapshot" }), fixture.snapshot);
  await page.waitForFunction(() => document.querySelector('input[aria-label="Find columns"]')?.value === "");
  assert.equal(await page.getByRole("textbox", { name: "Find columns", exact: true }).inputValue(), "", "Switching snapshots resets stale column search and pagination");

  await page.evaluate(workspace => { window.fixture.setSnapshot(undefined); window.fixture.setWorkspace({ ...workspace, imported_report_id: "fixture-report", kind: "imported_report" }); }, fixture.workspace);
  await page.getByRole("button", { name: "Sources", exact: true }).click();
  const managed = page.getByRole("region", { name: "Power BI-managed sources", exact: true });
  await managed.getByText(/No local data snapshot is available/).waitFor();
  assert.equal(await page.getByRole("button", { name: "Add source", exact: true }).count(), 0);
  await managed.getByRole("button", { name: "Open in Power BI Desktop", exact: true }).click();
  await managed.getByRole("button", { name: "Import updated PBIX", exact: true }).click();
  assert.deepEqual(await page.evaluate(() => [window.fixture.openedDesktop, window.fixture.importedReport]), [1, 1]);
  await capture("powerbi-managed-sources-no-snapshot-1280");
  assert.deepEqual(errors, []);
  console.log("PASS: readiness recovery, connector help/conditional action, native-driver prerequisite guard, stale/duplicate request guards, scoped late-result rejection, query-free saved import review, exact retry, reload-only import recovery, exact large identifiers, snapshot reset and truthful Power BI-managed sources. Real components; all operations mocked.");
} catch (error) { await capture("data-source-regression-failure"); if (errors.length) console.error(errors); throw error; }
finally { await browser.close(); }
