import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const load = async (file) => {
  const compiled = await build({ entryPoints: [new URL(`../src/${file}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" } });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
};
const { ModelInstallPanel, formatGb } = await load("ModelInstallCard.tsx");
const { RuntimeStatus } = await load("ProductUI.tsx");

const noop = () => {};
const base = { installed: false, running: false, download_available: true, bytes_total: 3389971840, local_copy: null };
const panel = (status) => renderToStaticMarkup(React.createElement(ModelInstallPanel, { status: { ...base, ...status }, busy: false, onStart: noop, onCancel: noop }));

test("a missing model offers one clear, sized install of Qwen 4B only", () => {
  const html = panel({ state: "missing" });
  assert.match(html, /Install Qwen 4B/);
  assert.match(html, /Download and install \(3\.4 GB\)/);
  assert.match(html, /checked before use/);
  assert.match(html, /I already have the model file/);
  assert.doesNotMatch(html, /Coder|1\.5 ?B|fallback|Light/i);
});

test("a copy already on this PC is preferred over downloading", () => {
  const html = panel({ state: "missing", local_copy: "C:\\Users\\me\\.ollama\\models\\blobs\\sha256-81fb" });
  assert.match(html, /Install from this PC \(no download\)/);
  assert.match(html, /Download instead/);
});

test("progress shows the activity, amount and speed with an accessible pause control", () => {
  const html = panel({ state: "downloading", running: true, bytes_done: 1694985920, bytes_per_second: 12500000 });
  assert.match(html, /role="status"/);
  assert.match(html, /role="progressbar"[^>]*aria-valuenow="50"/);
  assert.match(html, /Downloading Qwen 4B · 1\.7 GB of 3\.4 GB \(50%\) · 12\.5 MB\/s/);
  assert.match(html, /Pause/);
  assert.doesNotMatch(html, /Download and install/);
});

test("failures and pauses explain what happens next", () => {
  const failed = panel({ state: "failed", error: "The file does not match the pinned Qwen 4B fingerprint, so it was not installed." });
  assert.match(failed, /role="alert"/);
  assert.match(failed, /fingerprint/);
  assert.match(failed, /Try again/);
  assert.match(panel({ state: "cancelled", source: "download" }), /Resume download/);
  assert.match(panel({ state: "cancelled", source: "file" }), /Setup paused/);
  assert.doesNotMatch(panel({ state: "cancelled", source: "file" }), /Downloaded progress is kept/);
});

test("verified installation confirms readiness and zero progress stays zero", () => {
  assert.match(panel({ state: "installed", installed: true }), /Local AI is ready/);
  assert.equal(panel({ state: "unavailable", download_available: false }), "");
  assert.equal(formatGb(3389971840), "3.4 GB");
  assert.equal(formatGb(0), "0.0 GB");
});

test("checking an existing model never claims readiness and can be retried after cancellation", () => {
  const html = panel({ state: "verifying", source: "existing", running: true, installed: true, bytes_done: 0 });
  assert.match(html, /Checking your local AI model/);
  assert.match(html, /0\.0 GB of 3\.4 GB \(0%\)/);
  assert.match(html, /Cancel check/);
  assert.doesNotMatch(html, /Local AI is ready|Download and install/);
  const cancelled = panel({ state: "cancelled", source: "existing" });
  assert.match(cancelled, /Check model/);
  assert.doesNotMatch(cancelled, /Resume download/);
});

test("a damaged model offers repair without asking the user to delete files", () => {
  const damaged = panel({ state: "damaged", error: "The model could not be verified." });
  assert.match(damaged, /Repair local AI/);
  assert.match(damaged, /Download and repair \(3\.4 GB\)/);
  assert.match(damaged, /Your workspaces and reports are kept/);
  assert.match(panel({ state: "damaged", local_copy: "C:\\Models\\qwen" }), /Repair from this PC/);
  assert.doesNotMatch(damaged, /Local AI is ready/);
});

test("the top bar turns a missing model into an install action", () => {
  const status = (props) => renderToStaticMarkup(React.createElement(RuntimeStatus, props));
  const html = status({ ready: false, error: "Qwen 4B is not installed yet. Install it from Settings.", installNeeded: true, onInstall: noop });
  assert.match(html, /<button[^>]*>.*Install Qwen 4B<\/button>/);
  assert.match(status({ ready: false, error: "Missing runtime file", installNeeded: false, onInstall: noop }), /Local model unavailable/);
  assert.match(status({ ready: true, installNeeded: true, onInstall: noop }), /Local AI ready/);
  assert.match(status({ ready: false, error: "Checking the installed model.", onInstall: noop }), /Checking local AI/);
  assert.match(status({ ready: false, error: "The model is damaged. Repair it.", onInstall: noop }), /Repair local AI/);
  assert.match(status({ ready: true, setupState: "verifying", onInstall: noop }), /Checking local AI/);
  assert.doesNotMatch(status({ ready: true, setupState: "verifying", onInstall: noop }), /Local AI ready/);
  assert.match(status({ ready: true, setupState: "damaged", onInstall: noop }), /Repair local AI/);
  assert.match(status({ ready: false, setupState: "downloading", onInstall: noop }), /Setting up local AI/);
  assert.match(status({ ready: false, setupState: "cancelled", onInstall: noop }), /Resume local AI setup/);
});
