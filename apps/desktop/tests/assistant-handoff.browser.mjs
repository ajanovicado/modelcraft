// Real UI, isolated fixture APIs. No user's workspace, LLM or Power BI writes.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
const workspace = { ...fixture.workspace, chat_boundaries: [2] };
const question = "Create a total sales measure for the report";
const messages = [
  { id: 1, role: "user", content: "Unrelated archived workspace discussion", citations: [], created_at: fixture.timestamp },
  { id: 2, role: "assistant", content: "Older workspace answer", citations: [], created_at: fixture.timestamp },
  { id: 3, role: "user", content: question, citations: [], created_at: fixture.timestamp },
  { id: 4, role: "assistant", content: "Test this sales measure.\n\n```dax\nTotal Sales = SUM(Orders[Sales])\n```", citations: [], created_at: fixture.timestamp },
];
const event = (sequence, type, payload) => ({ sequence, type, payload: { snapshot_id: fixture.snapshot.id, ...payload }, created_at: `2025-07-01T12:${String(sequence).padStart(2, "0")}:00Z` });
const events = [
  event(1, "ingestion.completed", {}),
  event(2, "dax.assistant_requested", { request_id: "old-dax", goal: "Calculate regional sales" }),
  event(3, "dax.assistant_answered", { request_id: "old-dax", status: "completed", mode: "advice", response: "Archived DAX answer" }),
  event(4, "dax.chat.cleared", { through_sequence: 3 }),
  event(5, "dax.assistant_requested", { request_id: "current-dax", goal: "Explain our sales measure" }),
  event(6, "dax.assistant_answered", { request_id: "current-dax", status: "completed", mode: "advice", response: "Current DAX answer" }),
];
for (let index = 0; index < 16; index++) {
  const requestId = `long-dax-${index}`;
  events.push(event(events.length + 1, "dax.assistant_requested", { request_id: requestId, goal: `Preserved DAX question ${index}` }));
  events.push(event(events.length + 1, "dax.assistant_answered", { request_id: requestId, status: "completed", mode: "advice", response: `Preserved DAX context ${index}` }));
}
const browser = await chromium.launch({ ...(process.env.STUDIO_TEST_BROWSER ? { channel: process.env.STUDIO_TEST_BROWSER } : {}), headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
page.setDefaultTimeout(15_000);
const errors = [];
const requests = [];
const daxRequests = [];
let daxCleared = false;
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
  requests.push(`${method} ${path}`);
  try {
    if (method === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (method === "GET") {
      if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "chat-clear", "dax-chat-clear"] });
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
    if (path === `/v1/snapshots/${fixture.snapshot.id}/preview`) return json({ columns: [], rows: [], offset: 0, total_rows: 6, snapshot_rows: 6, has_more: false });
    if (path === `/v1/workspaces/${workspace.id}/dax/evaluate`) return json({ columns: ["Sales"], rows: [{ Sales: 1050 }], row_count: 1, truncated: false, elapsed_ms: 1, plan: "Fixture preview" });
    if (path === "/v1/dax/assist") {
      const body = request.postDataJSON();
      daxRequests.push(body);
      if (!daxCleared) {
        assert.ok(body.discussion.includes(question), "Explicit handoff request must become DAX context");
        assert.ok(!body.discussion.includes("Unrelated archived workspace"));
      } else assert.equal(body.discussion, undefined, "New chat detaches handoff and archived DAX context");
      const response = `DAX fixture reply ${daxRequests.length}`;
      events.push(event(events.length + 1, "dax.assistant_requested", { request_id: body.request_id, goal: body.goal }));
      events.push(event(events.length + 1, "dax.assistant_answered", { request_id: body.request_id, status: "completed", mode: "advice", response }));
      return route.fulfill({ headers, contentType: "text/event-stream", body: `event: assistant\ndata: {"kind":"advice"}\n\nevent: token\ndata: ${JSON.stringify({ text: response })}\n\nevent: done\ndata: {}\n\n` });
    }
    if (path === `/v1/workspaces/${workspace.id}/dax/chat/clear`) {
      assert.equal(request.postDataJSON().snapshot_id, fixture.snapshot.id);
      events.push(event(events.length + 1, "dax.chat.cleared", { through_sequence: events.length }));
      daxCleared = true;
      return json(workspace);
    }
    throw new Error(`Unmocked API blocked: ${method} ${path}`);
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
  await page.locator(".workspace-trigger").click();
  await page.getByRole("menuitem").filter({ has: page.locator("strong", { hasText: workspace.name }) }).click();
  const assistant = page.locator(".unified-conversation");
  const scope = assistant.locator(".assistant-scope");
  const mainComposer = page.getByRole("textbox", { name: "Message Local AI", exact: true });
  const daxComposer = page.getByRole("textbox", { name: "Message DAX AI", exact: true });
  const editor = page.getByRole("textbox", { name: "DAX expression", exact: true });
  await mainComposer.waitFor();
  assert.equal(await scope.textContent(), "Workspace");
  assert.equal(await page.getByRole("button", { name: "Chat history", exact: true }).count(), 1);
  await mainComposer.fill("Preserved workspace draft");
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  const daxThread = page.getByRole("region", { name: "DAX assistant conversation", exact: true });
  await page.waitForFunction(() => {
    const thread = document.querySelector('.dax-thread[aria-label="DAX assistant conversation"]');
    return thread?.clientHeight && thread.scrollHeight > thread.clientHeight && thread.scrollHeight - thread.clientHeight - thread.scrollTop < 72;
  });
  await daxThread.evaluate(node => { node.scrollTop = 0; });
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  assert.equal(await daxThread.evaluate(node => node.scrollTop), 0, "Intentional scroll-back survives tab switching");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await page.getByRole("button", { name: "Chat history", exact: true }).click();
  const history = page.locator(".conversation-history-panel:visible");
  await history.getByRole("button").filter({ has: page.locator("strong", { hasText: "Calculate regional sales" }) }).click();
  await assistant.getByText("Archived DAX answer", { exact: true }).waitFor();
  assert.equal(await scope.textContent(), "DAX Lab");
  assert.equal(await mainComposer.isVisible(), false);
  assert.equal(await daxComposer.count(), 0);
  assert.equal(await editor.isVisible(), true);
  await assistant.getByRole("button", { name: "Return to current chat", exact: true }).click();
  await daxComposer.fill("Preserved DAX draft");
  await editor.fill("SUM(Orders[Sales]) + 1");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  assert.equal(await mainComposer.inputValue(), "Preserved workspace draft");
  await page.getByRole("button", { name: "Continue in DAX Lab", exact: true }).click();
  await assistant.getByRole("region", { name: "Workspace request context", exact: true }).locator("p").getByText(question, { exact: true }).waitFor();
  assert.equal(await scope.textContent(), "DAX Lab");
  assert.equal(await mainComposer.isVisible(), false);
  assert.equal(await daxComposer.inputValue(), "Preserved DAX draft");
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1", "Handoff requires explicit Use formula");
  await page.getByRole("region", { name: "Suggested DAX formula", exact: true }).getByRole("button", { name: "Use formula", exact: true }).click();
  await page.getByText("1 returned row", { exact: false }).waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])");
  await daxComposer.fill("Explain this formula");
  await page.getByRole("button", { name: "Send DAX message", exact: true }).click();
  await assistant.getByText("DAX fixture reply 1", { exact: true }).waitFor();
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await assistant.getByRole("button", { name: "Back to source chat", exact: true }).waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])", "Scratchpad and handoff survive reload");
  await capture("assistant-dax-handoff-desktop");
  await assistant.getByRole("button", { name: "Back to source chat", exact: true }).click();
  await mainComposer.waitFor();
  assert.equal(await scope.textContent(), "Workspace");
  assert.equal(await mainComposer.inputValue(), "Preserved workspace draft");
  await page.waitForFunction(() => document.activeElement?.classList.contains("chat-message"));
  await page.getByRole("button", { name: "Chat history", exact: true }).click();
  await capture("assistant-shared-history-desktop");
  await history.getByRole("button").filter({ has: page.locator("strong", { hasText: "Explain our sales measure" }) }).click();
  await daxComposer.waitFor();
  await page.getByRole("button", { name: "Hide conversation", exact: true }).click();
  assert.equal(await daxComposer.isVisible(), false);
  await page.getByRole("button", { name: "Explain", exact: true }).click();
  await assistant.getByText("DAX fixture reply 2", { exact: true }).waitFor();
  assert.equal(await daxComposer.isVisible(), true, "Explain reveals hidden assistant");
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await assistant.getByText("DAX fixture reply 2", { exact: true }).waitFor({ state: "hidden" });
  assert.equal(await assistant.getByRole("button", { name: "Back to source chat", exact: true }).count(), 0);
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])");
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await page.getByRole("button", { name: "Continue in DAX Lab", exact: true }).click();
  await assistant.getByRole("button", { name: "Back to source chat", exact: true }).waitFor();
  const clearRequests = requests.filter(request => request.includes("/dax/chat/clear")).length;
  await page.getByRole("button", { name: "New chat", exact: true }).click();
  await assistant.getByRole("button", { name: "Back to source chat", exact: true }).waitFor({ state: "hidden" });
  assert.equal(requests.filter(request => request.includes("/dax/chat/clear")).length, clearRequests, "Handoff-only New chat must not archive an empty conversation");
  await page.reload({ waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "DAX Lab", exact: true }).click();
  await daxComposer.waitFor();
  assert.equal(await assistant.getByRole("button", { name: "Back to source chat", exact: true }).count(), 0, "Detached source stays detached after reload");
  await daxComposer.fill("A fresh formula question");
  await page.getByRole("button", { name: "Send DAX message", exact: true }).click();
  await assistant.getByText("DAX fixture reply 3", { exact: true }).waitFor();
  for (const width of [800, 390]) {
    await page.setViewportSize({ width, height: 900 });
    assert.equal(await scope.isVisible(), true);
    assert.equal(await daxComposer.isVisible(), true);
    assert.equal(await page.getByRole("button", { name: "New chat", exact: true }).count(), 1);
    assert.equal(await page.getByRole("button", { name: "Chat history", exact: true }).count(), 1);
    const box = await assistant.boundingBox();
    assert.ok(box.x >= 0 && box.x + box.width <= width + 1);
    await capture(`assistant-dax-${width}`);
  }
  assert.equal(await page.locator("vite-error-overlay").count(), 0);
  assert.deepEqual(errors, []);
  console.log(`PASS: one scoped assistant, cross-scope history, preserved drafts/editor, explicit handoff preview, original-request grounding, source return, Explain reveals assistant, New chat detaches context, desktop/800/390 layouts. ${requests.length} fixture API requests; no real writes.`);
} catch (error) {
  await capture("assistant-handoff-failure");
  throw new Error(`${error}\nApp errors: ${errors.join("; ")}`, { cause: error });
} finally {
  await browser.close();
}
