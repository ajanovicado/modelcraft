import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const load = async (file) => {
  const compiled = await build({ entryPoints: [new URL(`../src/${file}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
};
const { memoryDetail } = await load("memoryStatus.ts");
const { RuntimeStatus } = await load("ProductUI.tsx");

// Qwen 4B is the only local model and RAM or power never pause it: memory is reported,
// never presented as a requirement, a pause or a smaller fallback model.
const PAUSE_WORDING = /paused|needed to start|free another|waiting for memory|low memory|plug in|\blight\b|coder|1\.5 ?b|fallback|smallest/i;

test("memory detail reports usable RAM without a startup requirement", () => {
  assert.equal(memoryDetail(15.72), "15.7 GiB usable RAM · Model unloads before Desktop launch");
  for (const total of [undefined, null, 0, -1, Number.NaN]) assert.equal(memoryDetail(total), "Model unloads before Desktop launch");
  assert.doesNotMatch(memoryDetail(7.6), PAUSE_WORDING);
});

test("runtime status names readiness only, never a memory or power pause", () => {
  const status = (props) => renderToStaticMarkup(React.createElement(RuntimeStatus, props));
  assert.match(status({ ready: true }), /Local AI ready/);
  // Guard diagnostics from an engine that predates this change stay a plain tooltip.
  for (const error of ['Adaptive memory guard: {"available":1073741824,"missing":1073741824}', "AC power is required for local AI", "Missing runtime file: model.gguf"]) {
    const html = status({ ready: false, error });
    assert.match(html, /Local model unavailable/);
    assert.ok(html.includes(`title="${error.replaceAll('"', "&quot;")}"`));
    assert.doesNotMatch(html.replace(/title="[^"]*"/, ""), PAUSE_WORDING);
  }
});
