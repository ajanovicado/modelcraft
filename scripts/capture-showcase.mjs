// Reproduce actual frontend screenshots using only deterministic, synthetic API fixtures.
// Browser plugin not available: regular Playwright. Never attach to a user's dev server.
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdir, stat } from "node:fs/promises";
import { createRequire } from "node:module";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";
import { stripVTControlCharacters } from "node:util";
import { injectDemoNotice, showcaseFixture } from "./showcase-fixture.mjs";
import { createShowcaseResponder } from "./showcase-api.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const desktop = join(root, "apps", "desktop");
const requireDesktop = createRequire(join(desktop, "package.json"));
const { chromium } = requireDesktop("playwright");
const vite = join(desktop, "node_modules", "vite", "bin", "vite.js");
const origin = "http://127.0.0.1:15179";
const output = resolve(root, "docs", "images");
const fixture = showcaseFixture();
const respond = createShowcaseResponder(fixture);
const errors = [];
const server = spawn(process.execPath, [vite, "--host", "127.0.0.1", "--port", "15179", "--strictPort"], {
  cwd: desktop, env: { ...process.env, VITE_STUDIO_API_URL: "http://127.0.0.1:19881", VITE_STUDIO_TOKEN: "synthetic-showcase-fixture-only" }, stdio: ["ignore", "pipe", "pipe"],
});
let serverOutput = "", browser;
server.stdout.on("data", chunk => { serverOutput += chunk; });
server.stderr.on("data", chunk => { serverOutput += chunk; });
server.on("error", error => { serverOutput += String(error); });
const paths = [];
try {
  let ready = false;
  const deadline = Date.now() + 30_000;
  while (!ready && Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(`Isolated showcase server failed:\n${serverOutput}`);
    try { ready = stripVTControlCharacters(serverOutput).includes(origin) && (await fetch(origin)).ok; } catch { /* Wait for this exact server. */ }
    if (!ready) await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!ready) throw new Error(`Showcase server did not become ready:\n${serverOutput}`);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1, serviceWorkers: "block", locale: "en-US", timezoneId: "America/New_York" });
  const page = await context.newPage();
  page.setDefaultTimeout(30_000);
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => { if (["error", "warning"].includes(message.type())) errors.push(message.text()); });
  // Only the development reload socket is mocked. Chromium blocks its local-network
  // handshake for fulfilled fixture documents; browser security settings stay unchanged.
  await page.routeWebSocket(/^ws:\/\/127\.0\.0\.1:15179\//, socket => socket.send(JSON.stringify({ type: "connected" })));
  await page.route(`${origin}/`, async route => {
    const result = await route.fetch();
    await route.fulfill({ response: result, body: injectDemoNotice(await result.text()) });
  });
  await page.route("**/v1/**", async route => {
    const request = route.request(), method = request.method(), path = new URL(request.url()).pathname;
    const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" };
    if (method === "OPTIONS") return route.fulfill({ headers, status: 204 });
    const result = respond(method, path, request.postData() ? request.postDataJSON() : {});
    if (result.status !== 200) errors.push(`Unmocked showcase API blocked: ${method} ${path}`);
    if (result.contentType === "application/json") return route.fulfill({ headers, status: result.status, json: result.body });
    return route.fulfill({ headers, status: result.status, contentType: result.contentType, body: result.body });
  });
  // A fresh browser context owns all storage; no saved user workspace is read.
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 90_000 });
  assert.equal(new URL(page.url()).origin, origin);
  assert.equal(await page.title(), "Modelcraft — Your AI workspace for Power BI");
  await page.getByRole("textbox", { name: "Page title", exact: true }).waitFor();
  await page.getByText("This is a prepared fixture answer for the UI showcase.", { exact: true }).waitFor();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  await page.locator(".designer-visual .vega-embed svg").first().waitFor();
  assert.equal(await page.locator(".designer-validation-summary").count(), 0, "Showcase must not contain an invalid draft layout");
  await mkdir(output, { recursive: true });
  const capture = async name => {
    assert.equal(await page.locator("vite-error-overlay").count(), 0);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: join(output, name), animations: "disabled" });
    paths.push({ file: `docs/images/${name}`, bytes: (await stat(join(output, name))).size });
  };
  await capture("showcase-report.png");
  await page.getByRole("button", { name: "Hide conversation", exact: true }).click();
  await page.getByRole("tab", { name: "Data", exact: true }).click();
  await page.getByRole("button", { name: "Columns", exact: true }).click();
  await page.getByRole("columnheader", { name: "Complete", exact: true }).waitFor();
  await capture("showcase-data.png");
  await page.getByRole("tab", { name: "Model", exact: true }).click();
  await page.getByRole("button", { name: "Fit model to view", exact: true }).click();
  await page.locator(".mwb-canvas-node").first().waitFor();
  await capture("showcase-model.png");
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await page.getByRole("textbox", { name: "DAX expression", exact: true }).fill("SUM('Orders'[Sales])");
  await page.getByRole("button", { name: "Run", exact: true }).click();
  await page.getByRole("region", { name: "DAX results", exact: true }).waitFor();
  await page.getByRole("button", { name: "Explain", exact: true }).click();
  await page.getByText("Prepared demo explanation", { exact: true }).waitFor();
  await page.locator(".dax-lab-schema summary").filter({ hasText: "Orders" }).click();
  await capture("showcase-dax.png");
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ viewport: "1600 × 1000", source: "Actual frontend; synthetic deterministic API fixtures. No AI inference or Power BI run.", checks: "Page identity, meaningful UI, no error overlay, no app/console errors; all API requests mocked.", files: paths }, null, 2));
} catch (error) {
  if (errors.length) console.error(errors);
  throw error;
} finally {
  await browser?.close();
  if (server.exitCode === null) server.kill();
}
