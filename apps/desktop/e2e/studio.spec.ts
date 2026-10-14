import { test, expect } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { join } from "node:path";

const backend = "http://127.0.0.1:18765";
const headers = { Authorization: "Bearer isolated-playwright-release-token" };
const emptyHash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

test("real local data flows through inventory, profiling, preview, approval and PBIP build", async ({ page, request }, testInfo) => {
  // Real-model planning calls and the PBIP build legitimately take several
  // minutes on modest hardware; allow triple the default budget.
  test.slow();
  const errors: string[] = [];
  const externalRequests: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (["http:", "https:"].includes(url.protocol) && url.hostname !== "127.0.0.1") externalRequests.push(request.url());
  });
  const source = join(process.env.STUDIO_RELEASE_QA_ROOT!, "Release Golden Data");
  await mkdir(source, { recursive: true });
  await writeFile(join(source, "orders.csv"), "Order ID,Order Date,Region,Sales,Profit\nA1,2025-01-01,East,100.5,20.25\nA2,2025-02-01,West,250,40\nA3,2025-03-01,East,149.5,29.75\n");
  await page.goto("/");
  await expect(page).toHaveTitle(/Power BI/i);
  await expect(page.getByRole("button", { name: "Choose local files", exact: true })).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept(source));
  await page.getByRole("button", { name: "Choose local files", exact: true }).click();
  // Explorer-style picker: type the path directly, then confirm with Scan.
  await page.getByRole("button", { name: "Type path" }).click();
  await page.locator("#path-browser-address-input").fill(source);
  await page.getByRole("button", { name: "Scan", exact: true }).click();
  await expect(page.getByText("orders.csv", { exact: true }).first()).toBeVisible();
  // Inventory alone must not ingest the source.
  const workspaces = await (await request.get(`${backend}/v1/workspaces`, { headers })).json();
  const workspace = workspaces.find((item: { name: string }) => item.name === "Release Golden Data");
  expect(workspace.active_snapshot_id).toBeNull();
  await page.getByRole("button", { name: /Import 1 selected/ }).click();
  // Ingestion holds busy while it auto-builds the executive overview, then
  // closes the conversation to focus the report — wait for that draft first.
  await expect(page.getByRole("heading", { name: "Dashboard Designer" })).toBeVisible({ timeout: 150_000 });
  // Chat requests revise the active draft rather than replacing it, so the
  // overview/trends/detail structure this test exercises is created through
  // the same deterministic draft API the chat action calls.
  const refreshed = await (await request.get(`${backend}/v1/workspaces/${workspace.id}`, { headers })).json();
  const drafted = await request.post(`${backend}/v1/drafts`, { headers, timeout: 180_000, data: {
    workspace_id: workspace.id,
    snapshot_id: refreshed.active_snapshot_id,
    prompt: "Create a sales and profit dashboard with an overview, trends and detail page.",
    idempotency_key: "release-e2e-multipage-draft",
    expected_base_hash: refreshed.base_hash,
  } });
  expect(drafted.ok()).toBe(true);
  await page.reload();
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Dashboard Designer" })).toBeAttached();
  await expect(page.locator(".designer-canvas")).toContainText("500");
  await expect(page.locator(".designer-canvas")).toContainText("90");
  await page.screenshot({ path: testInfo.outputPath("source-backed-designer.png") });
  await page.setViewportSize({ width: 1040, height: 700 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  // Properties are an optional overlay. The canvas must fill the available track.
  const compactLayout = await page.locator(".designer-layout").boundingBox();
  const compactCanvas = await page.locator(".designer-canvas-panel").boundingBox();
  expect(compactLayout).not.toBeNull();
  expect(compactCanvas).not.toBeNull();
  expect(Math.abs(compactLayout!.x + compactLayout!.width - compactCanvas!.x - compactCanvas!.width)).toBeLessThan(3);
  await page.screenshot({ path: testInfo.outputPath("minimum-window-designer.png") });
  await page.setViewportSize({ width: 1440, height: 900 });
  // The request names Detail while Overview is selected. It must edit Detail,
  // reveal its real-data chart, and never substitute a new Custom Analysis page.
  const beforeWorkspace = await (await request.get(`${backend}/v1/workspaces/${workspace.id}`, { headers })).json();
  const beforeDraft = await (await request.get(`${backend}/v1/drafts/${beforeWorkspace.active_draft_id}`, { headers })).json();
  await page.getByRole("textbox", { name: "Message Local AI" }).fill("on detail page, add appropriate visual blow the table");
  const revisionResponse = page.waitForResponse((response) => response.url().endsWith("/chat/stream") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  const acceptedRevision = await revisionResponse;
  expect(acceptedRevision.ok()).toBe(true);
  // The stream opens immediately; the real CPU model may still be selecting the visual.
  await expect(page.locator('.designer-page-list button[aria-current="page"]')).toContainText("Detail", { timeout: 120_000 });
  await expect(page.locator(".designer-canvas .designer-visual")).toHaveCount(2);
  await expect(page.getByText("Draft updated", { exact: true })).toBeVisible();
  const afterDraft = await (await request.get(`${backend}/v1/drafts/${beforeWorkspace.active_draft_id}`, { headers })).json();
  expect(afterDraft.pages.length).toBe(beforeDraft.pages.length);
  const detail = afterDraft.pages.find((item: { id: string }) => item.id === "detail");
  expect(detail.visuals[1].y).toBeGreaterThanOrEqual(detail.visuals[0].y + detail.visuals[0].height);
  expect(afterDraft.previews.find((item: { visual_id: string }) => item.visual_id === detail.visuals[1].id).rows).toHaveLength(2);
  const replay = await request.post(acceptedRevision.url(), { headers, data: acceptedRevision.request().postDataJSON() });
  expect(replay.ok()).toBe(true);
  expect(await replay.text()).toContain(`"draft_id":"${afterDraft.id}"`);
  expect((await (await request.get(`${backend}/v1/drafts/${afterDraft.id}`, { headers })).json()).base_hash).toBe(afterDraft.base_hash);
  for (const [index, prompt] of [
    "Add a visual to the Missing page",
    "Add a visual to Overview and Detail pages",
    "Add a visual below the table",
    "Add a visual above the table on the Detail page",
    "Add another chart below the table on the Detail page",
  ].entries()) {
    const rejected = await request.post(`${backend}/v1/drafts/${afterDraft.id}/revisions`, { headers, data: {
      workspace_id: workspace.id, prompt, idempotency_key: `invalid-visual-${index}`, expected_base_hash: afterDraft.base_hash,
    } });
    expect(rejected.status()).toBe(422);
    expect((await (await request.get(`${backend}/v1/drafts/${afterDraft.id}`, { headers })).json()).base_hash).toBe(afterDraft.base_hash);
  }
  await page.screenshot({ path: testInfo.outputPath("detail-visual-added.png") });
  for (const name of ["Data", "Model", "Report"]) {
    await page.getByRole("tab", { name, exact: true }).click();
    await expect(page.locator("main")).not.toBeEmpty();
  }
  for (const name of ["Activity & versions", "Settings", "Workspace"]) {
    await page.getByRole("navigation").getByRole("button", { name, exact: true }).click();
    await expect(page.locator("main")).not.toBeEmpty();
  }
  await page.getByRole("button", { name: "Review & build" }).click();
  await page.getByRole("button", { name: "Approve reviewed draft" }).click();
  await page.getByRole("button", { name: "Build in Power BI", exact: true }).click();
  await expect.poll(async () => {
    const builds = await (await request.get(`${backend}/v1/workspaces/${workspace.id}/builds`, { headers })).json();
    return builds[0]?.state;
  }, { timeout: 120_000 }).toBe("committed");
  await page.screenshot({ path: testInfo.outputPath("validated-build.png") });
  await page.reload();
  await expect(page.getByRole("tab", { name: "Report", exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "Report", exact: true }).click();
  await expect(page.locator(".designer-canvas")).toContainText("500");
  expect(errors).toEqual([]);
  expect(externalRequests).toEqual([]);
});

test("local Qwen streams a grounded response and cancellation leaves a usable composer", async ({ page, request }, testInfo) => {
  // The composer only mounts once the workspace has profiled data; seed that
  // state through the API exactly as the import flow does.
  const source = join(process.env.STUDIO_RELEASE_QA_ROOT!, "Chat Golden Data");
  await mkdir(source, { recursive: true });
  await writeFile(join(source, "orders.csv"), "Order ID,Region,Sales,Profit\nA1,East,100.5,20.25\nA2,West,250,40\n");
  const created = await request.post(`${backend}/v1/workspaces`, { headers, data: {
    name: "Chat Grounding QA", idempotency_key: "chat-grounding-workspace", expected_base_hash: emptyHash,
  } });
  expect(created.ok()).toBe(true);
  const chatWorkspace = await created.json();
  const folderSet = await request.post(`${backend}/v1/workspaces/${chatWorkspace.id}/folder`, { headers, data: {
    workspace_id: chatWorkspace.id, folder: source, idempotency_key: "chat-grounding-folder", expected_base_hash: chatWorkspace.base_hash,
  } });
  expect(folderSet.ok()).toBe(true);
  const inventory = await (await request.post(`${backend}/v1/inventory`, { headers, data: { folder: source } })).json();
  const items = inventory.map((item: { absolute_path: string }) => item.absolute_path);
  const beforeIngest = await (await request.get(`${backend}/v1/workspaces/${chatWorkspace.id}`, { headers })).json();
  const ingested = await request.post(`${backend}/v1/ingestions`, { headers, timeout: 180_000, data: {
    workspace_id: chatWorkspace.id, item_paths: items, idempotency_key: "chat-grounding-ingest", expected_base_hash: beforeIngest.base_hash,
  } });
  expect(ingested.ok()).toBe(true);
  await page.goto("/");
  const composer = page.getByRole("textbox", { name: "Message Local AI" });
  await expect(composer).toBeVisible();
  await composer.fill("In one sentence, name the Sales and Profit fields available in this dataset. Do not propose changes.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await expect(page.getByRole("button", { name: "Stop response" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Stop response" })).toBeHidden({ timeout: 120_000 });
  const answer = page.getByRole("article", { name: "Local AI response" }).last();
  await expect(answer).toContainText(/Sales/i);
  await expect(answer).toContainText(/Profit/i);
  await page.screenshot({ path: testInfo.outputPath("real-qwen-chat.png") });
  await composer.fill("Explain the available fields and the dashboard in detail.");
  await page.getByRole("button", { name: "Send message", exact: true }).click();
  await page.getByRole("button", { name: "Stop response" }).click();
  await expect(page.getByText("Response stopped.", { exact: true })).toBeVisible();
  await expect(composer).toBeEnabled();
  await expect(page.locator(".response-dots")).toHaveCount(0);
});

test("API rejects unauthenticated, stale and malformed mutations without new workspaces", async ({ request }) => {
  expect((await request.get(`${backend}/v1/ping`)).status()).toBe(401);
  const before = await (await request.get(`${backend}/v1/workspaces`, { headers })).json();
  const response = await request.post(`${backend}/v1/workspaces`, { headers, data: {
    name: "Must not exist", idempotency_key: "stale-workspace-test", expected_base_hash: "f".repeat(64),
  } });
  expect(response.status()).toBe(409);
  const malformed = await request.post(`${backend}/v1/workspaces`, { headers, data: {
    name: "Must not exist", idempotency_key: "unknown-workspace-test", expected_base_hash: emptyHash, run: "anything",
  } });
  expect(malformed.status()).toBe(422);
  const after = await (await request.get(`${backend}/v1/workspaces`, { headers })).json();
  expect(after.length).toBe(before.length);
});
