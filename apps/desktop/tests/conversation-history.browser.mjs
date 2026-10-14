// The real frontend with fixture-only APIs: never talks to the user's engine or workspace.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
const browserName = process.env.STUDIO_TEST_BROWSER || "chromium";
const browser = await chromium.launch({ ...(browserName === "chromium" ? {} : { channel: browserName }), headless: true });
const viewport = { width: Number(process.env.STUDIO_TEST_WIDTH || 1440), height: 900 };
const page = await browser.newPage({ viewport, serviceWorkers: "block" });
page.setDefaultTimeout(15_000);
const errors = [];
const mocked = [];
const workspace = structuredClone(fixture.workspace);
const mainQuestion = fixture.messages[0].content;
const mainResponse = fixture.messages[1].content;
const formattedMainResponse = `${mainResponse}\n\n1. Review the sales calculation.\n2. Check the regional totals.\n\n| Region | Sales |\n| --- | --- |\n| East | 490 |\n| West | 560 |\n\n\`\`\`dax\nTotal Sales = SUM(Orders[Sales])\n\`\`\``;
const previousDataQuestion = "Previous dataset summary";
const messages = [
  { id: 1, role: "user", content: previousDataQuestion, citations: [], created_at: "2025-06-30T12:00:00Z" },
  { id: 2, role: "assistant", content: "Older data notes.", citations: [], created_at: "2025-06-30T12:00:01Z" },
  ...fixture.messages.map((message, index) => ({ ...message, ...(index === 1 ? { content: formattedMainResponse } : {}), id: index + 3 })),
];
let clearAttempts = 0;
let cleared = 0;
let daxCleared = 0;
let daxRequests = 0;
let attemptedDax;
const daxQuestion = "Explain our sales measure.";
const daxResponse = "SUM adds the numeric Sales values.";
const events = [
  { sequence: 1, type: "ingestion.completed", payload: { snapshot_id: fixture.snapshot.id }, created_at: fixture.timestamp },
  { sequence: 2, type: "dax.assistant_requested", payload: { request_id: "dax-history-fixture", snapshot_id: fixture.snapshot.id, goal: daxQuestion }, created_at: "2025-07-01T12:02:00Z" },
  { sequence: 3, type: "dax.assistant_answered", payload: { request_id: "dax-history-fixture", snapshot_id: fixture.snapshot.id, status: "completed", mode: "advice", response: daxResponse }, created_at: "2025-07-01T12:02:01Z" },
];
const evidenceDirectory = process.env.STUDIO_TEST_EVIDENCE_DIR;
const capture = async (name, locator = page) => {
  if (!evidenceDirectory) return;
  await mkdir(evidenceDirectory, { recursive: true });
  await locator.screenshot({ path: join(evidenceDirectory, `${name}-${viewport.width}.png`), animations: "disabled" });
};
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => {
  const expectedFailure = (message.location().url.endsWith("/chat/clear") || message.location().url.endsWith("/v1/dax/assist")) && /503/.test(message.text());
  if (message.type() === "error" && !expectedFailure) errors.push(message.text());
});

await page.route("**/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  const headers = {
    "Access-Control-Allow-Origin": request.headers().origin || "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  };
  const json = body => route.fulfill({ headers, json: body });
  mocked.push(`${method} ${path}`);
  try {
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "chat-clear", "draft-restore", "dax-chat-clear"] });
      if (path === "/v1/health") return json(fixture.health);
      if (path === "/v1/model/install") return json({ installed: true, running: false, state: "installed", download_available: false });
      if (path === "/v1/workspaces") return json([workspace]);
      if (path === `/v1/workspaces/${workspace.id}`) return json(workspace);
      if (path === `/v1/snapshots/${fixture.snapshot.id}`) return json(fixture.snapshot);
      if (path === `/v1/drafts/${fixture.draft.id}`) return json(fixture.draft);
      if (path === `/v1/workspaces/${workspace.id}/messages`) return json(messages);
      if (path === `/v1/workspaces/${workspace.id}/events`) return json(events);
      if (path === `/v1/workspaces/${workspace.id}/builds` || path === "/v1/report-themes") return json([]);
    }
    if (path === `/v1/snapshots/${fixture.snapshot.id}/preview` && method === "POST") return json({ columns: [], rows: [], offset: 0, total_rows: 6, snapshot_rows: 6, has_more: false });
    if (path === `/v1/workspaces/${workspace.id}/chat/clear` && method === "POST") {
      const body = request.postDataJSON();
      assert.equal(body.workspace_id, workspace.id);
      assert.equal(body.expected_base_hash, workspace.base_hash);
      assert.ok(body.idempotency_key);
      clearAttempts++;
      if (clearAttempts === 1) return route.fulfill({ status: 503, headers, json: { detail: "The local engine is temporarily unavailable." } });
      assert.equal(cleared, 0, "A single successful New chat should create one archive");
      workspace.chat_boundaries.push(messages.at(-1).id);
      workspace.base_hash = "1".repeat(64);
      cleared++;
      return json(workspace);
    }
    if (path === `/v1/workspaces/${workspace.id}/dax/chat/clear` && method === "POST") {
      const body = request.postDataJSON();
      assert.equal(body.workspace_id, workspace.id);
      assert.equal(body.snapshot_id, fixture.snapshot.id);
      assert.equal(body.expected_base_hash, workspace.base_hash);
      assert.ok(body.idempotency_key);
      assert.equal(daxCleared, 0);
      events.push({ sequence: 4, type: "dax.chat.cleared", payload: { snapshot_id: fixture.snapshot.id, through_sequence: 3 }, created_at: "2025-07-01T12:03:00Z" });
      daxCleared++;
      return json(workspace);
    }
    if (path === "/v1/dax/assist" && method === "POST") {
      const body = request.postDataJSON();
      assert.equal(body.workspace_id, workspace.id);
      assert.equal(body.snapshot_id, fixture.snapshot.id);
      assert.equal(body.goal, "Help me check this formula");
      assert.equal(body.expression, "SUM(Orders[Sales])");
      assert.ok(body.request_id);
      assert.equal(body.discussion, undefined, "Archived DAX dialogue must not become context for a new chat");
      daxRequests++;
      if (daxRequests === 1) {
        attemptedDax = structuredClone(body);
        return route.fulfill({ status: 503, headers, json: { detail: "DAX assistant temporarily unavailable." } });
      }
      assert.equal(daxRequests, 2);
      assert.deepEqual(body, attemptedDax, "DAX retry must preserve the original prompt, formula and request ID through reload");
      events.push(
        { sequence: 5, type: "dax.assistant_requested", payload: { request_id: body.request_id, snapshot_id: fixture.snapshot.id, goal: body.goal }, created_at: "2025-07-01T12:04:00Z" },
        { sequence: 6, type: "dax.assistant_answered", payload: { request_id: body.request_id, snapshot_id: fixture.snapshot.id, status: "completed", mode: "advice", response: "New DAX chat received without archived context." }, created_at: "2025-07-01T12:04:01Z" },
      );
      return route.fulfill({ headers, contentType: "text/event-stream", body: 'event: assistant\ndata: {"kind":"advice"}\n\nevent: token\ndata: {"text":"New DAX chat received without archived context."}\n\nevent: done\ndata: {}\n\n' });
    }
    throw new Error(`Unmocked API call blocked: ${method} ${path}`);
  } catch (error) {
    errors.push(String(error));
    return route.fulfill({ status: 500, headers, json: { detail: String(error) } });
  }
});

try {
  const url = process.env.STUDIO_TEST_URL || "http://127.0.0.1:15174/";
  await page.goto(url, { waitUntil: "networkidle" });
  assert.equal(new URL(page.url()).origin, new URL(url).origin);
  assert.match(await page.title(), /Power BI/i);
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  await page.locator(".workspace-trigger").click();
  await page.getByRole("menuitem").filter({ has: page.locator("strong", { hasText: workspace.name }) }).click();
  const conversation = page.getByRole("region", { name: "Chat with Local AI" });
  const composer = conversation.getByRole("textbox", { name: "Message Local AI", exact: true });
  await conversation.getByText(mainResponse, { exact: true }).waitFor();
  await conversation.getByRole("table").waitFor();
  assert.deepEqual(await conversation.getByRole("columnheader").allTextContents(), ["Region", "Sales"]);
  assert.equal(await conversation.locator(".rich-text ol li").count(), 2);
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(url).origin });
  await conversation.getByRole("button", { name: "Copy code", exact: true }).click();
  await conversation.getByRole("button", { name: "Code copied", exact: true }).waitFor();
  assert.equal(await page.evaluate(() => navigator.clipboard.readText()), "Total Sales = SUM(Orders[Sales])");
  await capture("main-chat-answer", conversation);
  await composer.fill("Keep this question for the next chat");
  const newChat = conversation.getByRole("button", { name: "New chat", exact: true });
  await newChat.click();
  await page.getByText("The local engine is temporarily unavailable.", { exact: true }).waitFor();
  assert.equal(cleared, 0);
  assert.equal(await composer.inputValue(), "Keep this question for the next chat");
  assert.ok(await conversation.getByText(mainResponse, { exact: true }).isVisible());
  await newChat.waitFor({ state: "visible" });
  await newChat.click();
  await conversation.getByText(mainResponse, { exact: true }).waitFor({ state: "hidden" });
  assert.equal(cleared, 1);
  assert.equal(await composer.inputValue(), "Keep this question for the next chat");
  assert.ok(await newChat.isDisabled(), "Empty chats should not create another archive");
  await conversation.getByRole("button", { name: "Chat history", exact: true }).click();
  const history = conversation.getByRole("region", { name: "Chat history", exact: true });
  const search = history.getByRole("searchbox", { name: "Search chats", exact: true });
  await search.waitFor();
  assert.equal(await composer.count(), 0, "History browsing must not expose a sendable composer");
  await history.getByRole("button", { name: "DAX", exact: true }).click();
  await history.getByText(daxQuestion, { exact: true }).waitFor();
  assert.ok(await history.locator(".conversation-history-current").count() > 0, "The same history entry includes the current DAX chat");
  await history.getByRole("button", { name: "Workspace", exact: true }).click();
  assert.deepEqual(await history.locator(".conversation-history-row").filter({ hasNot: page.locator(".conversation-history-current") }).locator("strong").allTextContents(), [mainQuestion, previousDataQuestion], "Current-data and previous-data workspace chats remain readable, newest first");
  await search.fill("earlier");
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await page.getByRole("textbox", { name: "Message DAX AI", exact: true }).press("Escape");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  assert.ok(await history.isVisible(), "Escape in DAX must not close a hidden Workspace history panel");
  assert.equal(await search.inputValue(), "earlier", "Hidden history keeps its search and scope");
  assert.equal(await history.getByRole("button", { name: "Workspace", exact: true }).getAttribute("aria-pressed"), "true");
  await search.press("Escape");
  await composer.waitFor();
  await conversation.getByRole("button", { name: "Chat history", exact: true }).click();
  await search.fill("missing words");
  await history.getByText("No matching chats", { exact: true }).waitFor();
  await search.fill("earlier");
  const row = history.getByRole("button").filter({ has: page.locator("strong", { hasText: mainQuestion }) });
  await row.waitFor();
  await capture("main-chat-history", history);
  await row.click();
  await conversation.getByText(mainResponse, { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Conversation history", "Opening a saved chat puts focus in its read-only transcript");
  assert.equal(await composer.count(), 0, "Archived conversations must be read-only");
  assert.equal(await conversation.getByRole("button", { name: "Send message", exact: true }).count(), 0);
  assert.equal(clearAttempts, 2);
  await capture("main-chat-archive", conversation);
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await page.getByRole("textbox", { name: "Message DAX AI", exact: true }).press("Escape");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  assert.equal(await composer.count(), 0, "Escape in another chat must not leave the Workspace archive");
  assert.ok(await conversation.getByText(mainResponse, { exact: true }).isVisible());
  await conversation.getByRole("region", { name: "Conversation history", exact: true }).press("Escape");
  assert.equal(await composer.inputValue(), "Keep this question for the next chat");
  await page.reload({ waitUntil: "networkidle" });
  await composer.waitFor();
  assert.equal(await composer.inputValue(), "Keep this question for the next chat", "Workspace composer draft survives reload");
  assert.equal(await conversation.getByText(mainResponse, { exact: true }).count(), 0);
  await conversation.getByRole("button", { name: "Chat history", exact: true }).click();
  await history.getByText(mainQuestion, { exact: true }).waitFor();
  await history.getByRole("button", { name: "Back to current chat", exact: true }).click();

  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  const dax = page.getByRole("region", { name: "DAX conversation", exact: true });
  const editor = page.getByRole("textbox", { name: "DAX expression", exact: true });
  const daxComposer = dax.getByRole("textbox", { name: "Message DAX AI", exact: true });
  await dax.getByText(daxResponse, { exact: true }).waitFor();
  await editor.fill("SUM(Orders[Sales])");
  await daxComposer.fill("Help me check this formula");
  await dax.getByRole("button", { name: "New chat", exact: true }).click();
  await dax.getByText(daxResponse, { exact: true }).waitFor({ state: "hidden" });
  assert.equal(daxCleared, 1);
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])");
  assert.equal(await daxComposer.inputValue(), "Help me check this formula");
  const emptyDaxNewChat = dax.getByRole("button", { name: "New chat", exact: true });
  if (await emptyDaxNewChat.isEnabled()) await emptyDaxNewChat.click();
  assert.equal(daxCleared, 1, "An empty DAX chat must not produce another archive");
  await dax.getByRole("button", { name: "Chat history", exact: true }).click();
  const daxHistory = dax.locator(".conversation-history-panel");
  await daxHistory.getByRole("button", { name: "Workspace", exact: true }).click();
  await daxHistory.getByText(mainQuestion, { exact: true }).waitFor();
  await daxHistory.getByRole("button", { name: "DAX", exact: true }).click();
  await daxHistory.getByRole("searchbox", { name: "Search chats", exact: true }).fill("numeric");
  assert.equal(await daxComposer.count(), 0);
  await daxHistory.getByRole("button").filter({ has: page.locator("strong", { hasText: daxQuestion }) }).click();
  await dax.getByText(daxResponse, { exact: true }).waitFor();
  assert.equal(await daxComposer.count(), 0);
  assert.equal(await dax.getByRole("button", { name: "Send DAX message", exact: true }).count(), 0);
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])");
  await capture("dax-chat-archive");
  await dax.getByRole("button", { name: "Return to current chat", exact: true }).click();
  assert.equal(await daxComposer.inputValue(), "Help me check this formula");
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await daxComposer.waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])");
  assert.equal(await daxComposer.inputValue(), "Help me check this formula");
  // Remove only the DAX history cache. The fixture engine's events must recover the archive.
  await page.evaluate(() => {
    for (const key of Object.keys(localStorage)) if (key.startsWith("studio.dax.archive.") || key.startsWith("studio.dax.conversation.")) localStorage.removeItem(key);
  });
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await dax.getByRole("button", { name: "Chat history", exact: true }).click();
  await daxHistory.getByText(daxQuestion, { exact: true }).waitFor();
  await capture("dax-chat-history", daxHistory);
  await daxHistory.getByRole("button", { name: "Back to current chat", exact: true }).click();
  assert.equal(await dax.getByText(daxResponse, { exact: true }).count(), 0);
  assert.equal(await daxComposer.inputValue(), "Help me check this formula");
  await dax.getByRole("button", { name: "Send DAX message", exact: true }).click();
  const retryDax = dax.locator(".dax-chat-recovery").getByRole("button", { name: "Retry", exact: true });
  await retryDax.waitFor();
  assert.equal(daxRequests, 1);
  await editor.fill("SUM(Orders[Sales]) + 1");
  await daxComposer.fill("A separate next question");
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await retryDax.waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1");
  assert.equal(await daxComposer.inputValue(), "A separate next question");
  await capture("dax-chat-recovery");
  await retryDax.click();
  await dax.getByText("New DAX chat received without archived context.", { exact: true }).waitFor();
  assert.equal(daxRequests, 2);
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1", "A recovered request must not replace a separately edited formula");
  assert.equal(await daxComposer.inputValue(), "A separate next question");
  assert.equal(messages.length, 4, "DAX conversations must not append to workspace chat");
  const stoppedQuestion = "Draft a sales formula, then stop";
  events.push(
    { sequence: 7, type: "dax.assistant_requested", payload: { request_id: "dax-stopped-fixture", snapshot_id: fixture.snapshot.id, goal: stoppedQuestion }, created_at: "2025-07-01T12:05:00Z" },
    { sequence: 8, type: "dax.assistant_answered", payload: { request_id: "dax-stopped-fixture", snapshot_id: fixture.snapshot.id, status: "cancelled", mode: "formula", response: "```dax\nPartial Sales = SUM(Orders[Sales])\n```" }, created_at: "2025-07-01T12:05:01Z" },
  );
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  const stoppedTurn = dax.locator(".dax-turn").filter({ hasText: stoppedQuestion });
  await stoppedTurn.getByText("Stopped response · Not completed", { exact: true }).waitFor();
  assert.equal(await stoppedTurn.locator("code").textContent(), "Partial Sales = SUM(Orders[Sales])");
  assert.equal(await stoppedTurn.getByRole("button", { name: "Use formula", exact: true }).count(), 0, "A stopped partial response must not provide a formula application action");
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1");
  assert.equal(await daxComposer.inputValue(), "A separate next question");
  await capture("dax-chat-stopped");
  assert.deepEqual(errors, []);
  console.log(`PASS (${browserName}, ${viewport.width}×${viewport.height}): workspace New chat recovery/preserved draft, searchable read-only histories, return/reload persistence, durable DAX archive recovery without browser history caches, formula preservation, archived dialogue excluded from new requests, DAX retry keeps its original request through reload without replacing later formula/composer edits, stopped partial response has no formula application action. ${mocked.length} API requests mocked, no backend or user workspace writes.`);
} catch (error) {
  await capture("chat-history-failure");
  throw new Error(`${error}\nMocked: ${mocked.join(", ")}\nApp errors: ${errors.join("; ")}`, { cause: error });
} finally {
  await browser.close();
}
