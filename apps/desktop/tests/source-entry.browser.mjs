// Full first-run app + native-model DAX controls, with fixture-only operations.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15175/").origin;
const workspace = { ...fixture.workspace, name: "Database workspace", active_snapshot_id: null, active_draft_id: null };
const nativeWorkspace = { ...workspace, name: "Native report fixture", kind: "imported_report", imported_report_id: "native-no-rows-report" };
const nativeReport = { id: nativeWorkspace.imported_report_id, workspace_id: nativeWorkspace.id, name: "Native report without saved rows", format: "PBIR", source_hash: "f".repeat(64), pages: [],
  measures: [{ TableName: "Orders", Name: "Existing Sales", Expression: "SUM(Orders[Sales])", FormatString: "#,0" }], relationships: [], tables: [{ TableName: "Orders", ColumnName: "Sales", PandasDataType: "float64" }], warnings: [] };
let importedReportMode = false;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
page.setDefaultTimeout(15000);
const errors = [], creates = [], requests = [], nativeEvaluations = [];
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => {
  if (message.type() === "error" && !(message.location().url.endsWith("/v1/workspaces") && /503/.test(message.text()))) errors.push(message.text());
});
await page.route("**/v1/**", async route => {
  const request = route.request(), method = request.method(), path = new URL(request.url()).pathname;
  const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" };
  const json = body => route.fulfill({ headers, json: body });
  if (method === "OPTIONS") return route.fulfill({ headers, status: 204 });
  requests.push(`${method} ${path}`);
  if (method === "GET") {
    if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "chat-clear", "dax-chat-clear"] });
    if (path === "/v1/health") return json(fixture.health);
    if (path === "/v1/model/install") return json({ installed: true, running: false, state: "installed", download_available: false });
    if (path === "/v1/workspaces") return json(importedReportMode ? [nativeWorkspace] : creates.length > 1 ? [workspace] : []);
    if (path === `/v1/workspaces/${workspace.id}`) return json(importedReportMode ? nativeWorkspace : workspace);
    if (importedReportMode && path === `/v1/workspaces/${workspace.id}/imported-report`) return json(nativeReport);
    if (importedReportMode && path === `/v1/workspaces/${workspace.id}/imported-report/history`) return json([]);
    if (importedReportMode && path === `/v1/workspaces/${workspace.id}/imported-report/tests`) return json({ tests: [], stale: false });
    if (path === `/v1/workspaces/${workspace.id}/messages` || path === `/v1/workspaces/${workspace.id}/events` || path === `/v1/workspaces/${workspace.id}/builds` || path === "/v1/report-themes") return json([]);
    if (path === "/v1/connections/drivers") return json([{ id: "snowflake", name: "Snowflake", installed: true, ready: true, detail: "Snowflake connector available; sign-in is checked only on connection." }, { id: "sqlserver", name: "SQL Server / Azure SQL", installed: true, ready: false, detail: "SQL Server ODBC Driver 17 or 18 is required." }]);
  }
  if (path === "/v1/workspaces" && method === "POST") {
    creates.push(request.postDataJSON());
    if (creates.length === 1) return route.fulfill({ headers, status: 503, json: { detail: "Fixture workspace creation temporarily unavailable." } });
    return json(workspace);
  }
  if (importedReportMode && path === `/v1/workspaces/${workspace.id}/dax/evaluate` && method === "POST") {
    const body = request.postDataJSON();
    nativeEvaluations.push(body);
    assert.equal(body.workspace_id, workspace.id);
    assert.equal(body.expression, "SUM(Orders[Sales])");
    assert.equal(body.engine, "powerbi", "Imported no-snapshot candidates must use the native model, not snapshot evaluation");
    return json({ columns: ["Value"], rows: [{ Value: 42 }], row_count: 1, elapsed_ms: 1, truncated: false, engine: "powerbi", plan: "Fixture native Power BI evaluation" });
  }
  errors.push(`Unmocked first-run request blocked: ${method} ${path}`);
  return route.fulfill({ headers, status: 500, json: { detail: "Unexpected source-entry fixture request." } });
});
const capture = async name => {
  if (!process.env.STUDIO_TEST_EVIDENCE_DIR) return;
  await mkdir(process.env.STUDIO_TEST_EVIDENCE_DIR, { recursive: true });
  await page.screenshot({ path: join(process.env.STUDIO_TEST_EVIDENCE_DIR, `${name}.png`), animations: "disabled" });
};

try {
  await page.goto(origin, { waitUntil: "networkidle" });
  assert.match(await page.title(), /Power BI/i);
  await page.getByRole("heading", { name: "Connect data or open a Power BI report", exact: true }).waitFor();
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  await capture("first-run-source-choices-1440");
  const start = page.getByRole("button", { name: "Connect to a database", exact: true });
  await start.evaluate(button => { button.click(); button.click(); });
  await page.getByText("Fixture workspace creation temporarily unavailable.", { exact: true }).waitFor();
  assert.equal(creates.length, 1, "A double-click must create one workspace request");
  await start.click();
  await page.getByRole("region", { name: "Database connections", exact: true }).waitFor();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  assert.deepEqual(creates[1], creates[0], "Workspace creation retry keeps its exact idempotency envelope");
  assert.equal(creates[0].name, "Database workspace");
  assert.ok(creates[0].idempotency_key);
  assert.equal(creates[0].selected_folder, undefined);
  assert.equal(creates[0].folder, undefined);
  assert.equal(requests.some(value => /inventory|compatibility|ingestions/.test(value)), false, "Direct database entry must not require or read a local folder");
  assert.equal(await page.getByRole("dialog").count(), 0);
  await page.setViewportSize({ width: 1280, height: 884 });
  await capture("folder-free-database-entry-1280");

  // Exercise the actual Model -> App suggestion -> DAX Lab -> API integration.
  importedReportMode = true;
  const beforeNative = requests.length;
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "Model", exact: true }).click();
  const model = page.getByRole("tabpanel", { name: "Model", exact: true });
  await model.getByRole("textbox", { name: "DAX formula of Existing Sales", exact: true }).waitFor();
  await model.getByRole("button", { name: "Try in DAX Lab", exact: true }).click();
  const suggestion = page.getByRole("region", { name: "Suggested DAX formula", exact: true });
  await suggestion.getByText("SUM(Orders[Sales])", { exact: true }).waitFor();
  assert.equal(nativeEvaluations.length, 0, "Opening a measure suggestion must not run its formula automatically");
  await suggestion.getByRole("button", { name: "Use formula", exact: true }).click();
  await page.getByRole("region", { name: "DAX results", exact: true }).getByText("Calculated by Power BI Desktop", { exact: true }).waitFor();
  assert.equal(await page.getByRole("textbox", { name: "DAX expression", exact: true }).inputValue(), "SUM(Orders[Sales])");
  assert.equal(nativeEvaluations.length, 1);
  assert.equal(requests.slice(beforeNative).some(value => /\/snapshots\/|\/dax\/assist|\/(edit|save|apply)$/.test(value)), false, "No local snapshot, AI call or report write belongs to native formula preview");
  await capture("native-model-measure-to-dax-1280");

  // The isolated scratchpad proves native-model evaluation without saved local rows.
  const transformed = await (await fetch(`${origin}/src/DaxLab.tsx`)).text();
  const transformedMain = await (await fetch(`${origin}/src/main.tsx`)).text();
  const reactPath = transformed.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)?.[1];
  const clientPath = transformedMain.match(/from\s+["']([^"']*\/react-dom_client\.js[^"']*)["']/)?.[1];
  assert.ok(reactPath && clientPath);
  const harness = `<!doctype html><html><head><meta charset="utf-8"><title>Native DAX source fixture</title></head><body><div id="root"></div><script type="module">
import React from ${JSON.stringify(reactPath)};
import ReactDOMClient from ${JSON.stringify(clientPath)};
import RefreshRuntime from "/@react-refresh";
import "/src/styles.css";
import "/src/studio-design.css";
import "/src/chat.css";
RefreshRuntime.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>type=>type;window.__vite_plugin_react_preamble_installed__=true;
const { DaxLab } = await import("/src/DaxLab.tsx");
localStorage.clear();
window.fixture = { runs: [], saves: [], asks: [] };
function Harness() {
  const [native, setNative] = React.useState(true);
  const [suggestion, setSuggestion] = React.useState("Candidate = SUM(Orders[Sales]) + 1");
  window.fixture.setNative = setNative;
  return React.createElement(DaxLab, { workspaceId: "native-source-fixture", busy: false,
    importedMeasures: native ? [{ Name: "Existing Sales", TableName: "Orders", Expression: "SUM(Orders[Sales])" }] : undefined,
    importedTables: ["Orders"], suggestion, suggestionLabel: "Reviewed native candidate", onDismissSuggestion: () => setSuggestion(undefined),
    onRun: async expression => { window.fixture.runs.push(expression); return { columns: ["Value"], rows: [{ Value: 42 }], row_count: 1, elapsed_ms: 1, truncated: false, engine: "powerbi", plan: "Fixture native evaluation" }; },
    onSave: async (...args) => { window.fixture.saves.push(args); throw new Error("Unexpected formula save in source fixture"); },
    onAsk: async (...args) => { window.fixture.asks.push(args); throw new Error("AI must remain unavailable without saved rows"); },
  });
}
ReactDOMClient.createRoot(document.getElementById("root")).render(React.createElement(Harness));
</script><style>html,body,#root{height:100%;margin:0}#root{padding:24px;background:#f5f5f7}#root>.dax-lab{height:100%;border:1px solid #ddd;border-radius:8px;background:white}</style></body></html>`;
  await page.context().grantPermissions(["local-network-access"], { origin });
  await page.route(`${origin}/__native-source-fixture`, route => route.fulfill({ contentType: "text/html", body: harness }));
  await page.goto(`${origin}/__native-source-fixture`, { waitUntil: "networkidle" });
  assert.equal(await page.title(), "Native DAX source fixture");
  const editor = page.getByRole("textbox", { name: "DAX expression", exact: true });
  await editor.fill("SUM(Orders[Sales])");
  await page.getByText(/Native queries use this report's Power BI Desktop working copy/).waitFor();
  const run = page.getByRole("button", { name: "Run", exact: true });
  assert.equal(await run.isDisabled(), false);
  await run.click();
  await page.getByRole("region", { name: "DAX results", exact: true }).getByText("Calculated by Power BI Desktop", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Explain", exact: true }).isDisabled(), true);
  await page.getByRole("textbox", { name: "Message DAX AI", exact: true }).fill("Explain this native result");
  assert.equal(await page.getByRole("button", { name: "Send DAX message", exact: true }).isDisabled(), true);
  await page.getByRole("region", { name: "Suggested DAX formula", exact: true }).getByRole("button", { name: "Use formula", exact: true }).click();
  await page.waitForFunction(() => window.fixture.runs.length === 2);
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1");
  assert.deepEqual(await page.evaluate(() => window.fixture.runs), ["SUM(Orders[Sales])", "SUM(Orders[Sales]) + 1"]);
  assert.deepEqual(await page.evaluate(() => [window.fixture.saves, window.fixture.asks]), [[], []]);
  await capture("native-dax-no-snapshot-1280");
  await page.evaluate(() => window.fixture.setNative(false));
  await page.waitForFunction(() => [...document.querySelectorAll("button")].find(button => button.textContent.trim() === "Run")?.disabled);
  assert.equal(await run.isDisabled(), true, "A new workspace without a native model or saved rows must still reject evaluation");
  assert.deepEqual(errors, []);
  console.log("PASS: first-run source choices, folder-free workspace creation with exact retry/duplicate guard; full-App imported no-rows Model measure -> DAX suggestion -> explicit native evaluation; native no-snapshot Run/candidate controls, disabled AI and no-model evaluation. All engine/Power BI operations mocked.");
} catch (error) { await capture("source-entry-regression-failure"); if (errors.length) console.error(errors); throw error; }
finally { await browser.close(); }
