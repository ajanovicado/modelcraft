// Real DaxLab lifecycle with controlled parent props and fixture callbacks only.
// A remote clear or source replacement can arrive while a promise is in flight.
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";
import { designerForecastFixture } from "./fixtures/designer-forecast.mjs";

const fixture = designerForecastFixture();
const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15174/").origin;
const transformed = await (await fetch(`${origin}/src/DaxLab.tsx`)).text();
const transformedMain = await (await fetch(`${origin}/src/main.tsx`)).text();
const reactPath = transformed.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)?.[1];
const clientPath = transformedMain.match(/from\s+["']([^"']*\/react-dom_client\.js[^"']*)["']/)?.[1];
assert.ok(reactPath && clientPath, "Use the same Vite React modules as the actual frontend");

const harness = `<!doctype html><html><head><meta charset="utf-8"><title>DAX lifecycle fixture</title></head><body><div id="root"></div><script type="module">
import React from ${JSON.stringify(reactPath)};
import ReactDOMClient from ${JSON.stringify(clientPath)};
import RefreshRuntime from "/@react-refresh";
import "/src/styles.css";
import "/src/studio-design.css";
import "/src/chat.css";
import "/src/conversation-history.css";
import "/src/workbench-shell.css";
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$ = () => {};
window.$RefreshSig$ = () => type => type;
window.__vite_plugin_react_preamble_installed__ = true;
const { DaxLab } = await import("/src/DaxLab.tsx");
const { useState } = React;
const { createRoot } = ReactDOMClient;
const fixture = ${JSON.stringify(fixture)};
const baseHandoff = { request: "Calculate sales for West", sourceLabel: "Original Workspace chat", sourceMessageId: 3, code: "West Sales = SUM(Orders[Sales])", label: "From Workspace chat" };
window.fixture = { requests: [], previews: [], saves: [], gate: undefined };
localStorage.clear();
localStorage.setItem("studio.dax." + fixture.workspace.id, "SUM(Orders[Sales])");
function Harness() {
  const [handoff, setHandoff] = useState(baseHandoff);
  const [boundary, setBoundary] = useState(4);
  const [host, setHost] = useState(null);
  window.fixture.setHandoff = setHandoff;
  window.fixture.setBoundary = setBoundary;
  const onAsk = async (input, signal, onStatus) => {
    window.fixture.requests.push(structuredClone(input));
    if (input.goal === "Fail before persistence") throw new Error("Fixture service unavailable before persistence");
    if (input.goal.startsWith("Gated")) return new Promise((resolve, reject) => {
      window.fixture.gate = { signal, resolve: (text, kind = "formula") => resolve({ text, kind, requestId: input.requestId }), reject };
      onStatus("Waiting for fixture response");
    });
    return { kind: "advice", text: "Fixture follow-up completed", requestId: input.requestId };
  };
  return React.createElement("main", { className: "fixture-shell" },
    React.createElement("aside", { className: "unified-conversation" }, React.createElement("header", null, "Local AI · DAX Lab"), React.createElement("div", { ref: setHost, className: "fixture-chat-host" })),
    React.createElement(DaxLab, { workspaceId: fixture.workspace.id, snapshot: fixture.snapshot, draft: fixture.draft, busy: false, active: true, externalConversation: true, conversationHost: host, handoff, chatBoundary: boundary, durableHistory: true,
      onClearHandoff: () => setHandoff(undefined), onDismissSuggestion: () => {}, onBackToSource: () => {},
      onNewChat: async () => setBoundary(value => value + 10),
      onAsk, onRun: async expression => { window.fixture.previews.push(expression); return { columns: ["Value"], rows: [{ Value: 42 }], row_count: 1, elapsed_ms: 1, truncated: false }; },
      onSave: async (...args) => { window.fixture.saves.push(args); throw new Error("Unexpected save in lifecycle fixture"); },
    }));
}
createRoot(document.getElementById("root")).render(React.createElement(Harness));
</script><style>html,body,#root{height:100%;margin:0}.fixture-shell{display:grid;grid-template-columns:440px minmax(0,1fr);height:100%;background:#f5f5f7}.fixture-shell>.unified-conversation{display:flex;flex-direction:column;min-height:0;border-right:1px solid #ddd}.fixture-shell>.unified-conversation>header{padding:18px;font-weight:600;background:white}.fixture-chat-host{display:flex;flex:1;min-height:0}.fixture-chat-host>.dax-chat{flex:1}.fixture-shell>.dax-lab{min-height:0}@media(max-width:1280px){.fixture-shell{grid-template-columns:400px minmax(0,1fr)}}</style></body></html>`;

const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
// The fulfilled HTML fixture needs permission to load Vite's loopback HMR socket.
await page.context().grantPermissions(["local-network-access"], { origin });
page.setDefaultTimeout(15_000);
const errors = [];
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
await page.route(`${origin}/__dax-lifecycle-fixture`, route => route.fulfill({ contentType: "text/html", body: harness }));
await page.route("**/v1/**", route => {
  errors.push(`Unexpected API call blocked: ${route.request().method()} ${route.request().url()}`);
  return route.fulfill({ status: 500, json: { detail: "Lifecycle fixture blocks all actual API calls" } });
});
const capture = async name => {
  if (!process.env.STUDIO_TEST_EVIDENCE_DIR) return;
  await mkdir(process.env.STUDIO_TEST_EVIDENCE_DIR, { recursive: true });
  await page.screenshot({ path: join(process.env.STUDIO_TEST_EVIDENCE_DIR, `${name}.png`), animations: "disabled" });
};
const editor = page.getByRole("textbox", { name: "DAX expression", exact: true });
const composer = page.getByRole("textbox", { name: "Message DAX AI", exact: true });
const send = async message => { await composer.fill(message); await page.getByRole("button", { name: "Send DAX message", exact: true }).click(); };
const finish = async text => page.evaluate(text => window.fixture.gate.resolve(text), text);
const gateReady = async () => page.waitForFunction(() => Boolean(window.fixture.gate));

try {
  await page.goto(`${origin}/__dax-lifecycle-fixture`, { waitUntil: "networkidle" });
  await composer.waitFor();
  await send("Fail before persistence");
  const failed = page.locator(".dax-turn").filter({ has: page.locator(".dax-turn-goal", { hasText: "Fail before persistence" }) });
  await failed.getByText("Response failed · Not completed", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Retry", exact: true }).waitFor();
  await send("Follow up after the failure");
  await page.getByText("Fixture follow-up completed", { exact: true }).waitFor();
  assert.equal(await failed.count(), 1, "Replacing recovery with a fresh send must retain the earlier failed request");
  const requests = await page.evaluate(() => window.fixture.requests);
  assert.equal(requests[0].chatBoundary, 4);
  assert.equal(requests[0].sourceIdentity, undefined, "Local provenance sidecars must not enter the API envelope");
  assert.equal(requests[1].chatBoundary, 4);
  assert.ok(!requests[1].discussion.includes("Fail before persistence"), "Failed user/assistant evidence must not become follow-up model context");
  await capture("dax-failed-followup-retained");

  await send("Gated source request");
  await gateReady();
  await page.evaluate(() => window.fixture.setHandoff({ request: "Calculate sales for West", sourceLabel: "Original Workspace chat", sourceMessageId: 9, code: "Different West Sales = SUM(Orders[Sales]) + 2" }));
  await page.waitForFunction(() => document.querySelector(".dax-handoff-formula code")?.textContent.includes("Different West Sales"));
  await finish("```dax\nWest Sales = SUM(Orders[Sales]) + 1\n```");
  await page.getByText("Current formula retained. Use formula to preview this candidate.", { exact: true }).waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales])", "A changed source request keeps the old result as a candidate");
  assert.equal(await page.evaluate(() => window.fixture.previews.length), 0);
  await page.setViewportSize({ width: 1280, height: 884 });
  await capture("dax-changed-source-candidate-1280");
  const candidate = page.locator(".dax-turn").filter({ has: page.locator(".dax-turn-goal", { hasText: "Gated source request" }) });
  await candidate.getByRole("button", { name: "Use formula", exact: true }).click();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1", "Explicit use still previews the retained candidate");
  assert.deepEqual(await page.evaluate(() => window.fixture.previews), ["SUM(Orders[Sales]) + 1"]);

  await page.evaluate(() => { window.fixture.gate = undefined; });
  await send("Gated remote clear");
  await gateReady();
  await page.evaluate(() => window.fixture.setBoundary(14));
  await page.waitForFunction(() => window.fixture.gate.signal.aborted);
  await page.getByText("Let's work out your calculation", { exact: true }).waitFor();
  await finish("```dax\nStale Sales = SUM(Orders[Sales]) + 999\n```");
  await page.getByRole("button", { name: "Send DAX message", exact: true }).waitFor();
  assert.equal(await editor.inputValue(), "SUM(Orders[Sales]) + 1");
  assert.equal(await page.getByText("Stale Sales", { exact: false }).count(), 0, "Late completion from the cleared chat must not append or apply");
  assert.equal(await page.getByRole("button", { name: "Retry", exact: true }).count(), 0);
  await page.waitForFunction(({ workspaceId, snapshotId }) => localStorage.getItem(`studio.dax.pending.v1:${workspaceId}:${snapshotId}`) === null, { workspaceId: fixture.workspace.id, snapshotId: fixture.snapshot.id });
  await send("Current chat after remote clear");
  await page.getByText("Fixture follow-up completed", { exact: true }).waitFor();
  const last = await page.evaluate(() => window.fixture.requests.at(-1));
  assert.equal(last.chatBoundary, 14);
  assert.ok(!last.discussion.includes("Gated source request") && !last.discussion.includes("Follow up after the failure"), "Old local turns cannot leak through a remote clear");
  assert.deepEqual(await page.evaluate(() => window.fixture.saves), []);
  assert.deepEqual(errors, []);
  console.log("PASS: rejected DAX turns stay visible but leave model context; changed source keeps a candidate until explicit use; remote New chat aborts and discards late replies and stale local recovery. Real component, fixture callbacks only; no API writes.");
} catch (error) { await capture("dax-lifecycle-failure"); if (errors.length) console.error(errors); throw error; }
finally { await browser.close(); }
