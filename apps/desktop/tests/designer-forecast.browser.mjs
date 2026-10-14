// Run against Vite only: every API call is fulfilled from a static in-memory fixture.
// No backend, user workspace, model inference or Power BI build is used.
// STUDIO_TEST_BROWSER defaults to installed Playwright chromium; msedge is a local override.
// Vite needs VITE_STUDIO_TOKEN set to a dummy 16+ character value for api.ts initialization.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { dirname } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const browserName = process.env.STUDIO_TEST_BROWSER || "chromium";
const browser = await chromium.launch({ ...(browserName === "chromium" ? {} : { channel: browserName }), headless: true });
const viewport = { width: Number(process.env.STUDIO_TEST_WIDTH || 1864), height: 884 };
const page = await browser.newPage({ viewport, serviceWorkers: "block" });
page.setDefaultTimeout(15_000);
const errors = [];
const fixture = designerForecastFixture();
let current = structuredClone(fixture.draft);
const targetPage = fixture.draft.pages[0];
const targetVisual = targetPage.visuals[0];
const baselinePreviews = structuredClone(current.previews);
let messages = structuredClone(fixture.messages);
let saves = 0, chats = 0;
let attemptedChat;
const expectedNetworkErrors = [];
const mockedRequests = [];
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => {
  if (message.type() !== "error") return;
  if (message.location().url.endsWith("/v1/chat/stream") && /503/.test(message.text())) expectedNetworkErrors.push(message.text());
  else errors.push(message.text());
});
await page.route("**/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  const headers = {
    "Access-Control-Allow-Origin": request.headers().origin || "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
  };
  const json = body => route.fulfill({ json: body, headers });
  mockedRequests.push(`${method} ${path}`);
  try {
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "draft-restore"] });
      if (path === "/v1/health") return json(fixture.health);
      if (path === "/v1/model/install") return json({ installed: true, running: false, state: "installed", download_available: false });
      if (path === "/v1/workspaces") return json([fixture.workspace]);
      if (path === `/v1/workspaces/${fixture.workspace.id}`) return json(fixture.workspace);
      if (path === `/v1/snapshots/${fixture.snapshot.id}`) return json(fixture.snapshot);
      if (path === `/v1/drafts/${current.id}`) return json(current);
      if (path === `/v1/workspaces/${fixture.workspace.id}/messages`) return json(messages);
      if (path === `/v1/workspaces/${fixture.workspace.id}/builds`
        || path === `/v1/workspaces/${fixture.workspace.id}/events`
        || path === "/v1/report-themes") return json([]);
    }
    if (path === `/v1/snapshots/${fixture.snapshot.id}/preview` && method === "POST") return json({ columns: [], rows: [], offset: 0, total_rows: 6, snapshot_rows: 6, has_more: false });
    if (path === "/v1/chat/stream" && method === "POST") {
      const body = request.postDataJSON();
      assert.equal(body.workspace_id, fixture.workspace.id);
      assert.equal(body.message, "add a forcasting in this graph");
      assert.deepEqual(body.context, { page_id: targetPage.id, visual_id: targetVisual.id });
      assert.equal(body.expected_base_hash, current.base_hash);
      assert.ok(body.idempotency_key);
      chats++;
      if (chats === 1) {
        attemptedChat = structuredClone(body);
        return route.fulfill({ status: 503, headers, json: { detail: "Local model temporarily unavailable." } });
      }
      assert.deepEqual(body, attemptedChat, "Retry must preserve the exact failed request after the selected chart changes");
      messages = [
        ...fixture.messages,
        { id: 3, role: "user", content: body.message, citations: [], created_at: fixture.timestamp },
        { id: 4, role: "assistant", content: "Selected visual context received.", citations: [], created_at: fixture.timestamp },
      ];
      return route.fulfill({ headers, contentType: "text/event-stream", body: 'event: token\ndata: {"text":"Selected visual context received."}\n\nevent: done\ndata: {}\n\n' });
    }
    if (path === `/v1/drafts/${current.id}/dashboard` && method === "PUT") {
      const body = request.postDataJSON();
      assert.equal(body.workspace_id, fixture.workspace.id);
      assert.equal(body.expected_base_hash, current.base_hash);
      assert.ok(body.idempotency_key);
      current = { ...structuredClone(current), pages: body.pages, status: "draft", base_hash: (++saves).toString(16).padStart(64, "0") };
      current.semantic_model.approved = false;
      return json(current);
    }
    throw new Error(`Unmocked API call blocked: ${method} ${path}`);
  } catch (error) {
    errors.push(String(error));
    return route.fulfill({ status: 500, headers, json: { detail: String(error) } });
  }
});

try {
  const testUrl = process.env.STUDIO_TEST_URL || "http://127.0.0.1:5173/";
  await page.goto(testUrl, { waitUntil: "networkidle" });
  assert.equal(new URL(page.url()).origin, new URL(testUrl).origin);
  assert.match(await page.title(), /Power BI/i);
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  await page.locator(".workspace-trigger").click();
  const workspaceName = fixture.workspace.name;
  await page.getByRole("menuitem").filter({ has: page.locator("strong", { hasText: new RegExp(`^${workspaceName.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}$`) }) }).click();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  await page.getByRole("textbox", { name: "Page title", exact: true }).waitFor();
  await page.getByText(fixture.messages[0].content, { exact: true }).waitFor();
  const pageButton = page.locator(".designer-page-select").filter({ hasText: targetPage.title });
  await pageButton.click();
  await page.getByRole("button", { name: `Ask AI about page ${targetPage.title}`, exact: true }).click();
  const contextChip = page.getByRole("button", { name: "Remove attached context" });
  assert.equal((await contextChip.innerText()).includes(targetVisual.title), false);
  const visualButton = () => page.locator(`[data-visual-id="${targetVisual.id}"] .designer-visual-select`);
  await visualButton().click();
  await contextChip.filter({ hasText: targetVisual.title }).waitFor();
  assert.ok((await contextChip.innerText()).includes(targetVisual.title), "Selected chart replaces the earlier page attachment");
  await page.getByRole("textbox", { name: "Message Local AI", exact: true }).fill("add a forcasting in this graph");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  const retry = page.locator(".chat-response-outcome").getByRole("button", { name: "Retry", exact: true });
  await retry.waitFor();
  assert.equal(chats, 1);
  assert.deepEqual(messages, fixture.messages, "A rejected attempt must not be mistaken for persisted history");
  await page.getByText(fixture.messages[0].content, { exact: true }).waitFor();
  const otherVisual = targetPage.visuals[1];
  await page.locator(`[data-visual-id="${otherVisual.id}"] .designer-visual-select`).click();
  await contextChip.filter({ hasText: otherVisual.title }).waitFor();
  assert.ok((await contextChip.innerText()).includes(otherVisual.title));
  const recovery = page.locator(".chat-response-outcome");
  assert.match(await recovery.innerText(), /add a forcasting in this graph/);
  assert.match(await recovery.innerText(), /Retry target: .*Sales over time/);
  const composer = page.getByRole("textbox", { name: "Message Local AI", exact: true });
  await composer.fill("Keep my unfinished question");
  await page.reload({ waitUntil: "networkidle" });
  await retry.waitFor();
  assert.equal(await composer.inputValue(), "Keep my unfinished question", "Reload preserves the draft alongside the original failed request");
  await page.locator(`[data-visual-id="${otherVisual.id}"] .designer-visual-select`).click();
  await contextChip.filter({ hasText: otherVisual.title }).waitFor();
  assert.match(await recovery.innerText(), /Retry target: .*Sales over time/);
  if (process.env.STUDIO_TEST_SCREENSHOT) {
    await mkdir(dirname(process.env.STUDIO_TEST_SCREENSHOT), { recursive: true });
    await page.getByRole("region", { name: "Chat with Local AI" }).screenshot({ path: process.env.STUDIO_TEST_SCREENSHOT.replace(/\.png$/, "-retry.png"), animations: "disabled" });
  }
  await retry.click();
  await page.getByText("Selected visual context received.", { exact: true }).waitFor();
  assert.equal(chats, 2);
  assert.equal(await composer.inputValue(), "Keep my unfinished question", "Retry must preserve a separately typed next question");
  assert.equal(messages.filter(message => message.role === "user" && message.content === fixture.messages[0].content).length, 1, "Retry must not resend the older saved user message");
  await visualButton().click();

  const properties = page.getByRole("complementary", { name: "Visual properties" });
  const enabled = properties.getByRole("checkbox", { name: "Enable forecast", exact: true });
  await enabled.check();
  await composer.fill("Keep my unfinished question");
  const send = page.getByRole("button", { name: "Send message", exact: true });
  assert.ok(await send.isDisabled());
  await page.getByText("Save your report edits before sending a message.", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Review report edits", exact: true }).click();
  assert.equal(await composer.inputValue(), "Keep my unfinished question");
  assert.equal(chats, 2);
  const length = properties.getByRole("spinbutton", { name: "Forecast length (points)", exact: true });
  assert.equal(await length.inputValue(), "6");
  assert.equal(await length.getAttribute("min"), "1");
  assert.equal(await length.getAttribute("max"), "60");
  const save = page.getByRole("button", { name: "Save draft", exact: true });
  await length.fill("61");
  assert.equal(await length.inputValue(), "61", "Out-of-range input must remain visible for correction");
  assert.equal(await length.getAttribute("aria-invalid"), "true");
  const forecastError = properties.locator(".designer-field-error");
  await forecastError.waitFor();
  assert.match(await forecastError.innerText(), /1.*60/);
  assert.ok(await save.isDisabled());
  assert.equal(saves, 0);
  if (process.env.STUDIO_TEST_SCREENSHOT) {
    await properties.locator(".designer-forecast-settings").screenshot({ path: process.env.STUDIO_TEST_SCREENSHOT.replace(/\.png$/, "-invalid.png"), animations: "disabled" });
    await page.screenshot({ path: process.env.STUDIO_TEST_SCREENSHOT.replace(/\.png$/, "-invalid-desktop.png"), fullPage: true, animations: "disabled" });
  }
  await length.fill("");
  assert.equal(await length.inputValue(), "", "An unfinished numeric edit must not silently revert to a default");
  assert.equal(await length.getAttribute("aria-invalid"), "true");
  assert.ok(await forecastError.isVisible());
  assert.ok(await save.isDisabled());
  await page.locator(`[data-visual-id="${otherVisual.id}"] .designer-visual-select`).click();
  await visualButton().click();
  assert.equal(await length.inputValue(), "6", "Changing selection discards only the invalid transient field text");
  assert.equal(await forecastError.count(), 0);
  await length.fill("61");
  await page.getByRole("button", { name: "Undo report edit", exact: true }).click();
  assert.equal(await forecastError.count(), 0);
  assert.equal(await enabled.isChecked(), false);
  await page.getByRole("button", { name: "Redo report edit", exact: true }).click();
  assert.equal(await length.inputValue(), "6", "Redo restores a valid forecast without stale validation errors");
  await length.fill("61");
  await page.getByRole("button", { name: "Reset", exact: true }).click();
  assert.equal(await enabled.isChecked(), false);
  assert.equal(await forecastError.count(), 0);
  assert.equal(await save.count(), 0);
  await enabled.check();
  await length.fill("12");
  assert.notEqual(await length.getAttribute("aria-invalid"), "true");
  assert.equal(await forecastError.count(), 0);
  assert.ok(await save.isEnabled());
  const confidence = properties.getByRole("combobox", { name: "Confidence interval", exact: true });
  assert.equal(await confidence.inputValue(), "95");
  assert.deepEqual(await confidence.locator("option").evaluateAll(options => options.map(option => Number(option.value))), [75, 80, 85, 90, 95, 99]);
  await confidence.selectOption("80");
  await properties.getByText("Advanced forecast settings", { exact: true }).click();
  const ignoreLast = properties.getByRole("spinbutton", { name: "Ignore last (points)", exact: true });
  assert.equal(await ignoreLast.getAttribute("min"), "0");
  assert.equal(await ignoreLast.getAttribute("max"), "500");
  await ignoreLast.fill("2");
  const seasonality = properties.getByRole("spinbutton", { name: "Seasonality (points)", exact: true });
  assert.equal(await seasonality.getAttribute("min"), "1");
  assert.equal(await seasonality.getAttribute("max"), "120");
  await seasonality.fill("12");
  assert.ok(await page.locator(`[data-visual-id="${targetVisual.id}"] .designer-forecast-status`).isVisible());
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  const savedVisual = () => current.pages.find(item => item.id === targetPage.id).visuals.find(item => item.id === targetVisual.id);
  assert.deepEqual(savedVisual().forecast, { forecast_length: 12, confidence: 80, ignore_last: 2, seasonality: 12 });
  await visualButton().click();
  assert.ok(await enabled.isChecked());
  assert.equal(await length.inputValue(), "12");
  assert.match(await properties.innerText(), /preview shows historical values/);
  assert.equal(current.status, "draft");
  assert.equal(current.semantic_model.approved, false);
  assert.deepEqual(current.previews, baselinePreviews, "Forecast controls must preserve historical preview evidence");
  await page.locator(`[data-visual-id="${targetVisual.id}"] .chart svg, [data-visual-id="${targetVisual.id}"] .chart canvas`).first().waitFor();
  if (process.env.STUDIO_TEST_SCREENSHOT) {
    await mkdir(dirname(process.env.STUDIO_TEST_SCREENSHOT), { recursive: true });
    const dismissNotice = page.getByRole("button", { name: "Dismiss notification", exact: true });
    if (await dismissNotice.count()) await dismissNotice.click();
    const advanced = properties.locator(".designer-forecast-advanced");
    if (await advanced.getAttribute("open") !== null) await advanced.locator("summary").click();
    await properties.locator(".designer-forecast-settings").screenshot({ path: process.env.STUDIO_TEST_SCREENSHOT, animations: "disabled" });
    await page.locator(`[data-visual-id="${targetVisual.id}"]`).screenshot({ path: process.env.STUDIO_TEST_SCREENSHOT.replace(/\.png$/, "-preview.png"), animations: "disabled" });
  }

  await properties.getByRole("combobox", { name: "Chart style", exact: true }).selectOption("areaChart");
  assert.equal(await enabled.count(), 0);
  assert.equal(await page.locator(`[data-visual-id="${targetVisual.id}"] .designer-forecast-status`).count(), 0);
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  assert.equal(savedVisual().forecast, null);

  await visualButton().click();
  await properties.getByRole("combobox", { name: "Chart style", exact: true }).selectOption("lineChart");
  await enabled.check();
  await enabled.uncheck();
  assert.equal(await length.count(), 0);
  await enabled.check();
  await properties.getByRole("combobox", { name: "Visual kind", exact: true }).selectOption("category");
  assert.equal(await enabled.count(), 0);
  await page.getByRole("button", { name: "Save draft", exact: true }).click();
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  assert.equal(savedVisual().forecast, null);
  assert.equal(saves, 3);
  assert.ok(expectedNetworkErrors.length <= 1, "Only the deliberately rejected chat may report a failed resource");
  assert.deepEqual(current.previews, baselinePreviews);
  // A reviewed schema can lose its date type; the controls must explain the missing binding.
  Object.assign(savedVisual(), { recipe: "trend", visual_type: "lineChart", category_column_id: "order_date", forecast: null });
  fixture.snapshot.tables[0].table.columns.find(column => column.id === "order_date").data_type = "string";
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("textbox", { name: "Page title", exact: true }).waitFor();
  await visualButton().click();
  assert.ok(await enabled.isDisabled(), "Forecast must require a reviewed date axis");
  await properties.getByText("Choose a date field in Date axis to enable forecasting.", { exact: true }).waitFor();
  assert.equal(await enabled.isChecked(), false);
  assert.equal(saves, 3, "An invalid forecast binding must never trigger a save");
  assert.deepEqual(errors, []);
  assert.ok(mockedRequests.length > 0);
  console.log(`PASS (${browserName}, ${viewport.width}×${viewport.height}): selected chart overrides attached page, failed typo request retries its original context/key after selection changes, old history is not resent, unsaved edits explain blocked chat, invalid forecast inputs stay visible and block Save, selection/Undo/Redo/Reset clear transient errors, native forecast controls, historical preview disclosure, mock persistence, disable and style/recipe clearing, missing date axis disables forecast with an explanation. All ${mockedRequests.length} API requests mocked; three in-memory saves + one rejected/one completed mocked chat; no backend calls.`);
} catch (error) {
  if (process.env.STUDIO_TEST_FAILURE_SCREENSHOT) {
    await mkdir(dirname(process.env.STUDIO_TEST_FAILURE_SCREENSHOT), { recursive: true });
    await page.screenshot({ path: process.env.STUDIO_TEST_FAILURE_SCREENSHOT, fullPage: true, animations: "disabled" });
  }
  throw new Error(`${error}\nMocked API requests: ${mockedRequests.join(", ")}\nApp errors: ${errors.join("; ")}`, { cause: error });
} finally {
  await browser.close();
}
