// Full first-launch app: all model operations are mocked; no model or workspace is written.
// Browser plugin not available; regular Playwright provides rendered interaction evidence.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15178/").origin;
const fixture = designerForecastFixture();
const base = { installed: false, running: false, download_available: true, bytes_total: 3389971840, bytes_done: 0, local_copy: null };
const status = value => ({ ...base, ...value });
const browser = await chromium.launch({ headless: true });
const errors = [];
let currentPage;
const capture = async name => {
  if (!process.env.STUDIO_TEST_EVIDENCE_DIR) return;
  await mkdir(process.env.STUDIO_TEST_EVIDENCE_DIR, { recursive: true });
  await currentPage.screenshot({ path: join(process.env.STUDIO_TEST_EVIDENCE_DIR, `${name}.png`), animations: "disabled" });
};
const scenarios = [];

async function scenario(initial, width = 1440) {
  const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: "block" });
  const page = await context.newPage();
  currentPage = page;
  page.setDefaultTimeout(30_000);
  let state = status(initial);
  const starts = [], cancels = [];
  let onStart = async () => status({ state: "downloading", running: true, source: "download" });
  let installReads = 0;
  page.on("pageerror", error => errors.push(String(error)));
  page.on("console", message => { if (["error", "warning"].includes(message.type())) errors.push(message.text()); });
  await page.route("**/v1/**", async route => {
    const request = route.request(), method = request.method(), path = new URL(request.url()).pathname;
    const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" };
    const json = body => route.fulfill({ headers, json: body });
    if (method === "OPTIONS") return route.fulfill({ headers, status: 204 });
    const modelError = state.installed ? null : state.state === "verifying" ? "Checking the installed model before use." : state.state === "damaged" ? "The local model is damaged. Repair it." : "Qwen 4B is not installed yet. Install it from Settings.";
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "chat-clear", "dax-chat-clear", "draft-restore"] });
      if (path === "/v1/health") return json({ ...fixture.health, model_available: state.installed, model_name: "Qwen 4B", model_error: modelError });
      if (path === "/v1/model-health") return json({ model_available: state.installed, error: modelError, available_memory_gb: 8, total_memory_gb: 16 });
      if (path === "/v1/workspaces" || path === "/v1/report-themes") return json([]);
      if (path === "/v1/model/install") { installReads += 1; return json(state); }
    }
    if (method === "POST" && path === "/v1/model/install") {
      starts.push(request.postDataJSON());
      // Keep the request pending to test a second click before React updates the button.
      await new Promise(resolve => setTimeout(resolve, 250));
      state = await onStart(starts.at(-1));
      return json(state);
    }
    if (method === "POST" && path === "/v1/model/install/cancel") {
      cancels.push(path);
      await new Promise(resolve => setTimeout(resolve, 250));
      state = { ...state, running: false, state: "cancelled", error: null };
      return json(state);
    }
    errors.push(`Unmocked model-setup request blocked: ${method} ${path}`);
    return route.fulfill({ headers, status: 500, json: { detail: "Unexpected model-setup fixture request." } });
  });
  await page.goto(origin, { waitUntil: "domcontentloaded", timeout: 60_000 });
  assert.equal(new URL(page.url()).origin, origin);
  assert.equal(await page.title(), "Modelcraft — Your AI workspace for Power BI");
  await page.getByRole("heading", { name: "Connect data or open a Power BI report", exact: true }).waitFor();
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  scenarios.push({ context, page });
  return { page, starts, cancels, setState: value => { state = status(value); }, setStart: callback => { onStart = callback; }, reads: () => installReads, close: () => context.close() };
}

const doubleClick = locator => locator.evaluate(button => { button.click(); button.click(); });

try {
  // The flow under test is: first launch -> install/pause/resume -> verified local AI readiness.
  const missing = await scenario({ state: "missing" });
  await missing.page.getByRole("heading", { name: "Install Qwen 4B", exact: true }).waitFor();
  await capture("model-setup-missing-1440");
  await doubleClick(missing.page.getByRole("button", { name: "Download and install (3.4 GB)", exact: true }));
  await missing.page.getByRole("button", { name: "Pause", exact: true }).waitFor();
  await missing.page.getByText("Setting up local AI…", { exact: true }).waitFor();
  assert.deepEqual(missing.starts, [{ source: "download" }]);
  await missing.page.getByRole("status").filter({ hasText: "Downloading Qwen 4B" }).waitFor();
  assert.match(await missing.page.locator(".model-install-progress").innerText(), /0\.0 GB of 3\.4 GB \(0%\)/);
  missing.setState({ state: "downloading", running: true, source: "download", bytes_done: 1694985920, bytes_per_second: 12500000 });
  await missing.page.getByRole("progressbar").filter({ has: missing.page.locator('[style="width: 50%;"]') }).waitFor();
  await capture("model-setup-progress-1440");
  await doubleClick(missing.page.getByRole("button", { name: "Pause", exact: true }));
  await missing.page.getByRole("button", { name: "Resume download", exact: true }).waitFor();
  await missing.page.getByRole("button", { name: "Resume local AI setup", exact: true }).waitFor();
  assert.equal(missing.cancels.length, 1);
  missing.setStart(async () => status({ state: "installed", installed: true, source: "download", bytes_done: 3389971840 }));
  await missing.page.getByRole("button", { name: "Resume download", exact: true }).click();
  await missing.page.getByRole("heading", { name: "Local AI is ready", exact: true }).waitFor();
  await missing.page.getByText("Local AI ready", { exact: true }).waitFor();
  assert.equal(missing.starts.length, 2);
  await capture("model-setup-ready-1440");
  await missing.close();

  // Existing bytes stay unready until checking succeeds; cancellation and repair remain visible.
  const checking = await scenario({ state: "verifying", running: true, source: "existing" }, 1280);
  await checking.page.getByRole("heading", { name: "Checking your local AI model", exact: true }).waitFor();
  assert.equal(await checking.page.getByText("Local AI ready", { exact: true }).count(), 0);
  await checking.page.getByText("Checking local AI…", { exact: true }).waitFor();
  await capture("model-setup-checking-1280");
  await doubleClick(checking.page.getByRole("button", { name: "Cancel check", exact: true }));
  await checking.page.getByRole("button", { name: "Check model", exact: true }).waitFor();
  assert.equal(checking.cancels.length, 1);
  checking.setStart(async () => status({ state: "verifying", running: true, source: "existing" }));
  await checking.page.getByRole("button", { name: "Check model", exact: true }).click();
  await checking.page.getByRole("heading", { name: "Checking your local AI model", exact: true }).waitFor();
  assert.deepEqual(checking.starts, [{ source: "auto" }]);
  checking.setState({ state: "damaged", source: "existing", error: "The model could not be verified. Repair it to continue." });
  await checking.page.getByRole("heading", { name: "Repair local AI", exact: true }).waitFor();
  await checking.page.getByRole("button", { name: "Repair local AI", exact: true }).waitFor();
  await capture("model-setup-damaged-1280");
  checking.setStart(async () => status({ state: "downloading", running: true, source: "download" }));
  await doubleClick(checking.page.getByRole("button", { name: "Download and repair (3.4 GB)", exact: true }));
  await checking.page.getByRole("button", { name: "Pause", exact: true }).waitFor();
  assert.deepEqual(checking.starts, [{ source: "auto" }, { source: "download" }]);
  checking.setState({ state: "failed", source: "download", error: "The connection was interrupted. Try again." });
  await checking.page.getByRole("alert").filter({ hasText: "connection was interrupted" }).waitFor();
  checking.setStart(async () => status({ state: "installed", installed: true, source: "download" }));
  await checking.page.getByRole("button", { name: "Try again", exact: true }).click();
  await checking.page.getByRole("heading", { name: "Local AI is ready", exact: true }).waitFor();
  await checking.page.getByText("Local AI ready", { exact: true }).waitFor();
  assert.equal(checking.starts.length, 3);
  await checking.close();

  // A file repair carries only the chosen path, and a rejected file stays retryable.
  const file = await scenario({ state: "damaged", source: "existing", error: "The model could not be verified." }, 1280);
  await file.page.getByText("I already have the model file", { exact: true }).click();
  const path = file.page.getByRole("textbox", { name: "Full path to the Qwen 4B file", exact: true });
  await path.fill("  D:\\Models\\qwen.gguf  ");
  file.setStart(async () => status({ state: "failed", source: "file", error: "This file could not be verified. Choose the matching Qwen 4B file." }));
  await doubleClick(file.page.getByRole("button", { name: "Install this file", exact: true }));
  await file.page.getByRole("alert").filter({ hasText: "Choose the matching" }).waitFor();
  assert.deepEqual(file.starts, [{ source: "file", path: "D:\\Models\\qwen.gguf" }]);
  assert.equal(await path.inputValue(), "  D:\\Models\\qwen.gguf  ");
  file.setStart(async () => status({ state: "installed", installed: true, source: "file" }));
  await file.page.getByRole("button", { name: "Install this file", exact: true }).click();
  await file.page.getByRole("heading", { name: "Local AI is ready", exact: true }).waitFor();
  assert.equal(file.starts.length, 2);
  await file.close();

  assert.deepEqual(errors, []);
  console.log("PASS: missing/install/progress/pause/resume/ready, existing-model verification/cancel/recheck, damaged-model repair/failure/retry and local-file repair. Repeated clicks create one mutation; ready follows verification; 1440/1280 px, zero app/console errors. All model and workspace operations mocked.");
} catch (error) {
  await capture("model-setup-regression-failure");
  if (errors.length) console.error(errors);
  throw error;
} finally { await browser.close(); }
