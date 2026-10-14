// Run against a dev server with an existing populated workspace.
// All mutations are mocked or blocked. Existing data is read, never modified.
// STUDIO_TEST_WORKSPACE defaults to Superstore Local; no LLM or Desktop is launched.
import { chromium } from "playwright";
import assert from "node:assert/strict";

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
const errors = [];
let original, current, targetPage, chatMessages;
let mockedWrites = 0;
page.on("pageerror", error => errors.push(String(error)));
await page.route("**/v1/**", async route => {
  const request = route.request();
  const path = new URL(request.url()).pathname;
  const json = body => route.fulfill({ json: body });
  if (path === "/v1/ping") return json({ ready: true, features: ["chat-context", "draft-restore"] });
  if (request.method() === "GET") {
    if (current && path === `/v1/drafts/${current.id}`) return json(current);
    if (chatMessages && path === `/v1/workspaces/${current.workspace_id}/messages`) return json(chatMessages);
    const response = await route.fetch();
    if (/^\/v1\/drafts\/[^/]+$/.test(path) && response.ok()) original = await response.json();
    return route.fulfill({ response });
  }
  if (request.method() === "OPTIONS") return route.continue();
  if (path === "/v1/inventory" || path === "/v1/compatibility") return route.continue(); // read-only inspection
  if (path === "/v1/chat/stream") {
    const body = request.postDataJSON();
    assert.equal(body.context.page_id, targetPage.id);
    assert.equal(body.expected_base_hash, original.base_hash);
    assert.ok(body.idempotency_key);
    current = structuredClone(original);
    current.status = "draft";
    current.base_hash = "e".repeat(64);
    current.semantic_model.approved = false;
    current.pages.find(item => item.id === targetPage.id).title = "Contextual review test";
    chatMessages = [{ role: "user", content: body.message, citations: [], created_at: new Date().toISOString() },
      { role: "assistant", content: "Draft updated. No Power BI changes applied.", citations: [], created_at: new Date().toISOString() }];
    mockedWrites++;
    return route.fulfill({ contentType: "text/event-stream", body: `event: draft\ndata: ${JSON.stringify({ draft_id: current.id })}\n\nevent: token\ndata: {"text":"Draft updated."}\n\nevent: done\ndata: {}\n\n` });
  }
  if (current && path === `/v1/drafts/${current.id}/restore`) {
    const body = request.postDataJSON();
    assert.equal(body.restore_hash, original.base_hash);
    assert.equal(body.expected_base_hash, current.base_hash);
    current = structuredClone(original);
    current.status = "draft";
    current.semantic_model.approved = false;
    current.base_hash = "d".repeat(64);
    mockedWrites++;
    return json(current);
  }
  errors.push(`Unexpected mutation blocked: ${request.method()} ${path}`);
  return route.abort();
});

try {
  await page.goto(process.env.STUDIO_TEST_URL || "http://127.0.0.1:5173/", { waitUntil: "networkidle" });
  await page.locator(".workspace-trigger").click();
  await page.getByRole("menuitem").filter({ hasText: process.env.STUDIO_TEST_WORKSPACE || "Superstore Local" }).click();
  await page.waitForLoadState("networkidle");
  await page.waitForFunction(name => document.querySelector(".workspace-trigger")?.textContent.includes(name), process.env.STUDIO_TEST_WORKSPACE || "Superstore Local");
  await page.locator(".progress-banner").waitFor({ state: "hidden" });
  const title = page.getByRole("textbox", { name: "Page title", exact: true });
  await title.waitFor();
  const savedTitle = await title.inputValue();
  assert.equal(await page.locator("textarea:visible").count(), 1);
  await title.fill("Unsaved report edit");
  for (const view of ["Data", "Model", "Report"]) await page.getByRole("tab", { name: view, exact: true }).click();
  assert.equal(await title.inputValue(), "Unsaved report edit");
  assert.equal(await page.getByRole("button", { name: "Review & build", exact: true }).count(), 0);
  assert.ok(await page.locator(".unified-toolbar").getByRole("button", { name: "Save draft", exact: true }).isEnabled());
  for (const view of ["Activity & versions", "Workspace"]) await page.getByRole("navigation").getByRole("button", { name: view, exact: true }).click();
  assert.equal(await title.inputValue(), "Unsaved report edit");
  await page.getByRole("button", { name: "Reset", exact: true }).click();
  assert.equal(await title.inputValue(), savedTitle);
  targetPage = original.pages.at(-1);
  await page.locator(".designer-page-select").last().click();
  await page.getByRole("button", { name: `Ask AI about page ${targetPage.title}`, exact: true }).click();
  assert.equal(await title.inputValue(), targetPage.title);
  assert.match(await page.getByRole("button", { name: "Remove attached context" }).innerText(), new RegExp(targetPage.title));
  await page.getByRole("textbox", { name: /^Message local / }).fill("Rename this page Contextual review test");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.getByText("Draft updated", { exact: true }).waitFor();
  assert.equal(await title.inputValue(), "Contextual review test");
  await page.getByRole("button", { name: "Undo draft change", exact: true }).click();
  await page.getByText("Previous draft restored. Review it again before building. Power BI unchanged.", { exact: true }).waitFor();
  assert.equal(await title.inputValue(), targetPage.title);
  assert.equal(await page.getByRole("button", { name: "Remove attached context" }).count(), 0);
  await page.getByRole("button", { name: "Review & build", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Review changes" });
  await dialog.waitFor();
  assert.ok(await dialog.getByRole("button", { name: "Approve reviewed draft" }).isVisible());
  assert.equal(await dialog.getByRole("button", { name: "Build in Power BI", exact: true }).count(), 0);
  const duplicates = await page.evaluate(() => {
    const ids = [...document.querySelectorAll("[id]")].map(node => node.id);
    return ids.filter((id, index) => ids.indexOf(id) !== index);
  });
  assert.deepEqual(duplicates, []);
  await page.keyboard.press("Escape");
  assert.equal(await dialog.count(), 0);
  await page.waitForFunction(() => document.activeElement?.textContent === "Review & build");
  await page.setViewportSize({ width: 800, height: 900 });
  assert.ok(await page.getByRole("textbox", { name: /^Message local / }).isVisible());
  await page.getByRole("button", { name: "Hide conversation", exact: true }).click();
  assert.equal(await page.locator("textarea:visible").count(), 0);
  assert.equal(mockedWrites, 2);
  assert.deepEqual(errors, []);
  console.log("PASS: single conversation, persistent edits, scoped chat request, applied-change receipt, draft undo, approval reset, modal focus/IDs, narrow drawer. Two MOCKED mutations; zero real writes.");
} finally {
  await browser.close();
}
