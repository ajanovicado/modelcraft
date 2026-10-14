import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/desktopConnect.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { needsDesktop, nextCheckIn, OPENED_WAIT_MS, PASSIVE_WAIT_MS } = loaded.exports;

test("only the not-connected message starts the Desktop wait", () => {
  assert.ok(needsDesktop("Open this report in Power BI Desktop, wait for it to load, then refresh. Studio only connects to this report's working copy."));
  assert.ok(!needsDesktop("Install Power BI Desktop to open the native report"));
  assert.ok(!needsDesktop("Power BI could not evaluate this request: syntax error"));
  assert.ok(!needsDesktop("The visuals could not load."));
});

test("after opening Desktop the page checks often, backs off, then stops", () => {
  const wait = { since: 0, opened: true };
  assert.equal(nextCheckIn(wait, 10_000), 3_000);
  assert.equal(nextCheckIn(wait, 90_000), 6_000);
  assert.equal(nextCheckIn(wait, OPENED_WAIT_MS + 1), null);
});

test("without a click the page still notices a report opened by hand, slowly", () => {
  const wait = { since: 0, opened: false };
  assert.equal(nextCheckIn(wait, 5_000), 8_000);
  assert.equal(nextCheckIn(wait, PASSIVE_WAIT_MS + 1), null);
});
