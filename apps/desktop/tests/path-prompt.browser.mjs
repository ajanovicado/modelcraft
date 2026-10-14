// Explorer-style coverage for the in-app picker (PathPrompt in UnifiedWorkspace).
// Run against a dev server: STUDIO_TEST_URL defaults to http://127.0.0.1:5173/.
// Every /v1 call is stubbed in the browser, so no engine, model or real folder
// is read, and the dialog is closed with Escape so nothing is written.
import { chromium } from "playwright";
import assert from "node:assert/strict";

const HOME = "C:\\Users\\developer";
const DESKTOP = `${HOME}\\Desktop`;
const PROJECT = `${DESKTOP}\\AI-PowerBI-Analyst`;
const entries = (...items) => items.map(([name, path, kind, selectable]) => ({ name, path, kind, selectable }));
const tree = {
  "": { path: "", parent: null, truncated: false, entries: entries(["Home", HOME, "dir", false], ["C:\\", "C:\\", "dir", false], ["D:\\", "D:\\", "dir", false]) },
  [HOME]: { path: HOME, parent: "C:\\Users", truncated: false, entries: entries(
    ["Desktop", DESKTOP, "dir", false], ["Documents", `${HOME}\\Documents`, "dir", false], ["Public", `${HOME}\\Public`, "dir", false],
    ["budget.xlsx", `${HOME}\\budget.xlsx`, "file", true], ["readme.txt", `${HOME}\\readme.txt`, "file", false]) },
  [DESKTOP]: { path: DESKTOP, parent: HOME, truncated: false, entries: entries(["AI-PowerBI-Analyst", PROJECT, "dir", false], ["orders.csv", `${DESKTOP}\\orders.csv`, "file", true]) },
  [`${HOME}\\Documents`]: { path: `${HOME}\\Documents`, parent: HOME, truncated: false, entries: [] },
  [PROJECT]: { path: PROJECT, parent: DESKTOP, truncated: false, entries: entries(["src", `${PROJECT}\\src`, "dir", false]) },
};
const workspace = { id: "ws-picker", name: "New local workspace", kind: "new", base_hash: "0".repeat(64) };
const health = {
  status: "ready", version: "test", state_root: "C:\\state", model_available: true, model_name: "test",
  available_memory_gb: 16, powerbi_desktop_available: false, offline: true,
};

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 960 } });
const errors = [];
const browsePaths = [];
page.on("pageerror", (error) => errors.push(String(error)));
await page.route("**/v1/**", (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const json = (body) => route.fulfill({ json: body });
  if (url.pathname === "/v1/ping") return json({ ready: true, features: ["chat-context", "draft-restore", "chat-clear"] });
  if (url.pathname === "/v1/health") return json(health);
  if (url.pathname === "/v1/workspaces" && request.method() === "GET") return json([]);
  if (url.pathname === "/v1/workspaces" && request.method() === "POST") return json(workspace);
  if (url.pathname === "/v1/browse") {
    const path = url.searchParams.get("path") || "";
    browsePaths.push(path);
    return tree[path] ? json(tree[path]) : route.fulfill({ status: 400, json: { detail: "This folder cannot be listed" } });
  }
  return json([]);
});

try {
  await page.goto(process.env.STUDIO_TEST_URL || "http://127.0.0.1:5173/", { waitUntil: "domcontentloaded" });
  const trigger = page.getByRole("button", { name: /Choose (local data )?folder/i }).first();
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "Choose local data" });
  await dialog.waitFor();

  // Opens on This PC: drives are listed, and quick access is built from the same
  // read-only listing, so folders the engine cannot open are never offered.
  const places = dialog.getByRole("navigation", { name: "Quick access" });
  await places.getByRole("button", { name: "Desktop" }).waitFor();
  assert.deepEqual(await places.getByRole("button").allInnerTexts(), ["Home", "Desktop", "Documents", "C:\\", "D:\\"]);
  assert.equal(await dialog.locator(".path-browser-crumb-current").innerText(), "This PC");
  assert.deepEqual(await dialog.locator(".path-browser-name").allInnerTexts(), ["Home", "C:\\", "D:\\"]);

  // Quick access navigates and the breadcrumb becomes clickable segments.
  await places.getByRole("button", { name: "Desktop" }).click();
  await dialog.getByText("orders.csv", { exact: true }).waitFor();
  assert.deepEqual(await dialog.locator(".path-browser-crumb").allInnerTexts(), ["C:\\", "Users", "developer"]);
  assert.equal(await dialog.locator(".path-browser-crumb-current").innerText(), "Desktop");
  assert.equal(await places.getByRole("button", { name: "Desktop" }).getAttribute("aria-current"), "true");
  assert.equal(await dialog.locator(".path-browser-choice").innerText(), `Selected: ${DESKTOP}`);
  assert.ok(await dialog.getByRole("button", { name: "Scan" }).isEnabled());

  // Search narrows the list without asking the engine again.
  const search = dialog.getByRole("textbox", { name: "Search this folder" });
  const callsBeforeSearch = browsePaths.length;
  await search.fill("zzz");
  assert.equal(await dialog.locator(".path-browser-status").innerText(), "No match for “zzz” in this folder");
  await search.fill("ORD");
  assert.deepEqual(await dialog.locator(".path-browser-name").allInnerTexts(), ["orders.csv"]);
  assert.equal(browsePaths.length, callsBeforeSearch);
  await search.fill("");

  // Unsupported files stay visible but cannot become the choice.
  await places.getByRole("button", { name: "Home" }).click();
  await dialog.getByText("readme.txt", { exact: true }).waitFor();
  const blocked = dialog.getByRole("option", { name: /readme\.txt/ });
  assert.equal(await blocked.isDisabled(), true);
  assert.equal(await blocked.getAttribute("aria-disabled"), "true");
  assert.ok(await blocked.getByText("not supported").isVisible());

  // A supported file becomes the choice and is highlighted.
  await dialog.getByRole("option", { name: "budget.xlsx" }).click();
  assert.equal(await dialog.getByRole("option", { name: "budget.xlsx" }).getAttribute("aria-selected"), "true");
  assert.equal(await dialog.locator(".path-browser-choice").innerText(), `Selected: ${HOME}\\budget.xlsx`);

  // One click opens a folder; typing a path stays opt-in.
  await dialog.getByRole("option", { name: "Desktop" }).click();
  await dialog.getByText("AI-PowerBI-Analyst", { exact: true }).waitFor();
  await dialog.getByRole("option", { name: "AI-PowerBI-Analyst" }).click();
  await dialog.getByText("src", { exact: true }).waitFor();
  assert.equal(await dialog.locator(".path-browser-crumb-current").innerText(), "AI-PowerBI-Analyst");
  assert.ok(browsePaths.includes(PROJECT));
  assert.equal(await dialog.getByRole("textbox", { name: "Local path" }).count(), 0);
  await dialog.getByRole("button", { name: "Type path" }).click();
  const address = dialog.getByRole("textbox", { name: "Local path" });
  assert.equal(await address.inputValue(), PROJECT);

  // Typing a path and pressing Enter navigates instead of confirming.
  await address.fill(`${HOME}\\Documents`);
  await address.press("Enter");
  await dialog.getByText("This folder is empty", { exact: true }).waitFor();
  assert.ok(browsePaths.includes(`${HOME}\\Documents`));

  // Keyboard: Backspace returns to the parent and the list tracks one active option.
  await dialog.locator(".path-browser-list").focus();
  await page.keyboard.press("Backspace");
  await dialog.getByText("budget.xlsx", { exact: true }).waitFor();
  assert.equal(await dialog.locator(".path-browser-crumb-current").innerText(), "developer");
  await page.keyboard.press("ArrowDown");
  assert.equal(await dialog.locator(".path-browser-active .path-browser-name").innerText(), "Documents");
  await page.keyboard.press("Enter");
  await dialog.getByText("This folder is empty", { exact: true }).waitFor();
  assert.equal(await dialog.locator(".path-browser-crumb-current").innerText(), "Documents");
  await dialog.getByRole("button", { name: "Up" }).click();
  await dialog.getByText("budget.xlsx", { exact: true }).waitFor();

  const duplicates = await page.evaluate(() => {
    const ids = [...document.querySelectorAll("[id]")].map((node) => node.id);
    return ids.filter((id, index) => ids.indexOf(id) !== index);
  });
  assert.deepEqual(duplicates, []);
  if (process.env.STUDIO_PICKER_SCREENSHOT) await page.screenshot({ path: process.env.STUDIO_PICKER_SCREENSHOT });

  // Escape closes without confirming, and focus returns to the trigger.
  await page.keyboard.press("Escape");
  assert.equal(await dialog.count(), 0);
  await page.waitForFunction(() => /Choose/i.test(document.activeElement?.textContent || ""));
  assert.deepEqual(errors, []);
  console.log("PASS: Explorer-style picker — This PC and drives, quick access, breadcrumbs, one-click folders, search, unsupported files, opt-in address entry, keyboard navigation. Zero real engine calls.");
} finally {
  await browser.close();
}
