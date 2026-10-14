// Conversation lifecycle regressions against isolated fixture APIs, never user data.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
const workspace = { ...fixture.workspace, chat_boundaries: [2] };
const second = { ...workspace, id: "second-chat-workspace", name: "Second chat fixture", active_snapshot_id: "second-chat-snapshot", active_draft_id: undefined, chat_boundaries: [] };
const secondSnapshot = { ...fixture.snapshot, id: second.active_snapshot_id, workspace_id: second.id };
const messages = [
  { id: 1, role: "user", content: "Archived source question", created_at: fixture.timestamp, citations: [] },
  { id: 2, role: "assistant", content: "Archived source answer", created_at: fixture.timestamp, citations: [] },
  { id: 3, role: "user", content: "A stopped formula request", created_at: fixture.timestamp, citations: [] },
  { id: 4, role: "assistant", content: "Partial formula:\n\n```dax\nPartial Sales = SUM(Orders[Sales])\n```", status: "cancelled", request_id: "cancelled-request-01", created_at: fixture.timestamp, citations: [] },
];
const secondMessages = [{ id: 1, role: "user", content: "Second workspace current question", created_at: fixture.timestamp, citations: [] }];
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
page.setDefaultTimeout(15_000);
const errors = [];
const chatRequests = [];
let nextReplyStatus = "completed";
let releaseResponse;
let releaseHistory;
let historyReadPending = false;
let requestSeen;
const requested = new Promise(resolve => { requestSeen = resolve; });
const responseGate = new Promise(resolve => { releaseResponse = resolve; });
const historyGate = new Promise(resolve => { releaseHistory = resolve; });
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
const capture = async name => {
  if (!process.env.STUDIO_TEST_EVIDENCE_DIR) return;
  await mkdir(process.env.STUDIO_TEST_EVIDENCE_DIR, { recursive: true });
  await page.screenshot({ path: join(process.env.STUDIO_TEST_EVIDENCE_DIR, `${name}.png`), animations: "disabled" });
};
await page.route("**/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const method = request.method();
  const headers = { "Access-Control-Allow-Origin": request.headers().origin || "*", "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS" };
  const json = body => route.fulfill({ headers, json: body });
  try {
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "chat-clear", "dax-chat-clear"] });
      if (path === "/v1/health") return json(fixture.health);
      if (path === "/v1/model/install") return json({ installed: true, running: false, state: "installed", download_available: false });
      if (path === "/v1/workspaces") return json([workspace, second]);
      if (path === `/v1/workspaces/${workspace.id}`) return json(workspace);
      if (path === `/v1/workspaces/${second.id}`) return json(second);
      if (path === `/v1/snapshots/${fixture.snapshot.id}`) return json(fixture.snapshot);
      if (path === `/v1/snapshots/${secondSnapshot.id}`) return json(secondSnapshot);
      if (path === `/v1/drafts/${fixture.draft.id}`) return json(fixture.draft);
      if (path === `/v1/workspaces/${workspace.id}/messages`) {
        if (chatRequests.length && !historyReadPending) { historyReadPending = true; await historyGate; }
        return json(messages);
      }
      if (path === `/v1/workspaces/${second.id}/messages`) return json(secondMessages);
      if (path.endsWith("/events") || path.endsWith("/builds") || path === "/v1/report-themes") return json([]);
    }
    if (path.endsWith("/preview")) return json({ columns: [], rows: [], offset: 0, total_rows: 6, snapshot_rows: 6, has_more: false });
    if (path === "/v1/chat/stream") {
      const body = request.postDataJSON();
      chatRequests.push(body);
      requestSeen();
      await responseGate;
      const text = nextReplyStatus === "completed" ? "Completed lifecycle answer" : "A partial native model answer";
      const prior = messages.find(message => message.role === "assistant" && message.request_id === body.idempotency_key);
      if (prior) {
        prior.content = text;
        for (const message of messages.filter(message => message.request_id === body.idempotency_key)) message.status = nextReplyStatus;
      } else messages.push(
        { id: messages.length + 1, role: "user", content: body.message, request_id: body.idempotency_key, status: nextReplyStatus, created_at: fixture.timestamp, citations: [] },
        { id: messages.length + 2, role: "assistant", content: text, request_id: body.idempotency_key, status: nextReplyStatus, created_at: fixture.timestamp, citations: [] },
      );
      return route.fulfill({ headers, contentType: "text/event-stream", body: `event: token\ndata: ${JSON.stringify({ text })}\n\nevent: done\ndata: {}\n\n` });
    }
    throw new Error(`Unmocked API blocked: ${method} ${path}`);
  } catch (error) { errors.push(String(error)); return route.fulfill({ status: 500, headers, json: { detail: String(error) } }); }
});

try {
  const url = process.env.STUDIO_TEST_URL || "http://127.0.0.1:15174/";
  await page.goto(url, { waitUntil: "networkidle" });
  assert.equal(new URL(page.url()).origin, new URL(url).origin);
  assert.match(await page.title(), /Power BI/i);
  const choose = async name => {
    await page.locator(".workspace-trigger").click();
    await page.getByRole("menuitem").filter({ has: page.locator("strong", { hasText: name }) }).click();
  };
  await choose(workspace.name);
  const composer = page.getByRole("textbox", { name: "Message Local AI", exact: true });
  await composer.waitFor();
  const partial = page.locator(".chat-message").filter({ hasText: "Partial formula:" });
  await partial.waitFor();
  assert.equal(await partial.getByRole("button", { name: "Continue in DAX Lab", exact: true }).count(), 0, "Closed code fences from stopped requests must not be actionable");
  await capture("conversation-incomplete-response");
  await page.getByRole("button", { name: "Chat history", exact: true }).click();
  await page.locator(".conversation-history-panel").getByRole("button").filter({ has: page.locator("strong", { hasText: "Archived source question" }) }).click();
  await page.getByText("Archived source answer", { exact: true }).waitFor();
  await choose(second.name);
  await composer.waitFor();
  await page.getByText("Second workspace current question", { exact: true }).waitFor();
  assert.equal(await page.getByRole("button", { name: "Return to current chat", exact: true }).count(), 0, "Archive navigation cannot leak across workspace keys");
  await choose(workspace.name);
  await composer.waitFor();
  await partial.waitFor();
  const setRecovery = async (boundaryId, key) => page.evaluate(({ workspace, snapshotId, boundaryId, key }) => {
    localStorage.setItem(`studio.chatRecovery.v1:${workspace.id}:${snapshotId}`, JSON.stringify({ workspaceId: workspace.id, snapshotId, boundaryId, message: "Saved original request", context: null, baseHash: workspace.base_hash, key }));
  }, { workspace, snapshotId: fixture.snapshot.id, boundaryId, key });
  await setRecovery(0, "archived-request-01");
  await page.reload({ waitUntil: "networkidle" });
  await composer.waitFor();
  assert.equal(await page.locator(".chat-response-outcome").count(), 0, "New-chat boundaries invalidate saved old retry envelopes");
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (key.startsWith("studio.chatRecovery.")) throw new DOMException("Fixture storage quota", "QuotaExceededError");
      return original.call(this, key, value);
    };
    window.restoreFixtureStorage = () => { Storage.prototype.setItem = original; };
  });
  await composer.fill("One request from two immediate Enter presses");
  await composer.evaluate(node => {
    for (let index = 0; index < 2; index++) node.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true }));
  });
  await requested;
  await page.getByRole("button", { name: "Stop response", exact: true }).waitFor();
  await page.getByText("Retry details could not be saved on this device. Keep this tab open to retain the original question and context.", { exact: true }).waitFor();
  await page.evaluate(() => { window.restoreFixtureStorage(); delete window.restoreFixtureStorage; });
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(chatRequests.length, 1, "Same-tick sends cannot create two requests");
  assert.equal(chatRequests[0].chat_boundary, 2, "Requests carry the captured active chat boundary");
  releaseResponse();
  await page.getByText("Completed lifecycle answer", { exact: true }).waitFor();
  await choose(second.name);
  assert.equal(await page.getByText("Second workspace current question", { exact: true }).count(), 0, "Workspace switching stays blocked until transcript reconciliation completes");
  await page.keyboard.press("Escape");
  releaseHistory();
  await page.getByRole("button", { name: "Send message", exact: true }).waitFor();
  await setRecovery(2, chatRequests[0].idempotency_key);
  await page.reload({ waitUntil: "networkidle" });
  await composer.waitFor();
  assert.equal(await page.locator(".chat-response-outcome").count(), 0, "Server completion overrides a stale local interrupted receipt");
  nextReplyStatus = "interrupted";
  await composer.fill("A native model response that ends early");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  const recovery = page.locator(".chat-response-outcome");
  await recovery.getByText("The model returned an incomplete response. Retry the original question when you’re ready.", { exact: true }).waitFor();
  assert.equal(chatRequests.length, 2);
  await composer.fill("Preserve this separate next question");
  await page.reload({ waitUntil: "networkidle" });
  await recovery.getByRole("button", { name: "Retry", exact: true }).waitFor();
  nextReplyStatus = "completed";
  await recovery.getByRole("button", { name: "Retry", exact: true }).click();
  await recovery.waitFor({ state: "hidden" });
  assert.deepEqual(chatRequests[2], chatRequests[1], "Native incomplete recovery keeps its original context and request identity after reload");
  assert.equal(await composer.inputValue(), "Preserve this separate next question");
  await choose(second.name);
  await page.getByText("Second workspace current question", { exact: true }).waitFor();
  await capture("conversation-workspace-isolation");
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  assert.deepEqual(errors, []);
  console.log("PASS: incomplete replies cannot hand off, history navigation is workspace scoped, stale/completed recovery is reconciled, native incomplete completion retains exact retry through reload, same-tick send is single flight, transcript reconciliation blocks scope switching. Fixture APIs only; no real writes.");
} catch (error) { await capture("conversation-lifecycle-failure"); throw error; }
finally { releaseResponse(); releaseHistory(); await browser.close(); }
