import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

async function load(file) {
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../src/${file}`, import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" }, logLevel: "silent" });
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), module, module.exports);
  return module.exports;
}
const { AsyncActionLifetime } = await load("asyncAction.ts");
const { AsyncActionButton } = await load("AsyncActionButton.tsx");
const deferred = () => { let resolve; let reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

test("admission prevents a synchronous double click and a finished request cannot unlock its replacement", () => {
  const lifetime = new AsyncActionLifetime();
  const first = lifetime.begin("workspace/candidate/name");
  assert.ok(first);
  assert.equal(lifetime.begin("workspace/candidate/name"), undefined);
  assert.equal(lifetime.release(first), true);
  const next = lifetime.begin("workspace/candidate/name");
  assert.ok(next);
  assert.equal(lifetime.release(first), false);
  assert.equal(lifetime.owns(next, "workspace/candidate/name"), true);
  assert.equal(lifetime.begin("other form"), undefined);
});

test("late confirmation after owner or candidate change cannot mark the new action successful", async () => {
  const lifetime = new AsyncActionLifetime();
  let scope = "workspace-1/data-1/candidate-1/name";
  const first = lifetime.begin(scope);
  const saved = deferred();
  const outcomes = [];
  const completion = (async () => {
    try { await saved.promise; if (lifetime.owns(first, scope)) outcomes.push("saved"); }
    finally { lifetime.release(first); }
  })();
  scope = "workspace-2/data-2/candidate-2/name";
  lifetime.invalidate();
  const current = lifetime.begin(scope);
  saved.resolve();
  await completion;
  assert.deepEqual(outcomes, []);
  assert.equal(lifetime.owns(current, scope), true);
});

test("changed formula, table or name rejects completion even before scope cleanup", () => {
  const lifetime = new AsyncActionLifetime();
  const token = lifetime.begin(JSON.stringify(["w", "s", "SUM(Sales[Amount])", "Total", "Sales"]));
  for (const current of [
    ["w", "s", "SUM(Sales[Cost])", "Total", "Sales"],
    ["w", "s", "SUM(Sales[Amount])", "New total", "Sales"],
    ["w", "s", "SUM(Sales[Amount])", "Total", "Measures"],
    ["w", "other snapshot", "SUM(Sales[Amount])", "Total", "Sales"],
  ]) assert.equal(lifetime.owns(token, JSON.stringify(current)), false);
});

test("rejection stays a failed outcome and a real retry can then confirm success", async () => {
  const lifetime = new AsyncActionLifetime();
  const scope = "original form";
  const outcomes = [];
  async function attempt(action) {
    const token = lifetime.begin(scope);
    try { await action(); if (lifetime.owns(token, scope)) outcomes.push("success"); }
    catch { if (lifetime.owns(token, scope)) outcomes.push("error"); }
    finally { lifetime.release(token); }
  }
  await attempt(async () => { throw new Error("Rejected queue receipt"); });
  assert.deepEqual(outcomes, ["error"]);
  await attempt(async () => undefined);
  assert.deepEqual(outcomes, ["error", "success"]);
});

test("unmount invalidation ignores a late failure and remains invalid on returning to the same form", async () => {
  const lifetime = new AsyncActionLifetime();
  const original = lifetime.begin("same form");
  const pending = deferred();
  const outcomes = [];
  const completion = (async () => {
    try { await pending.promise; }
    catch { if (lifetime.owns(original, "same form")) outcomes.push("error"); }
    finally { lifetime.release(original); }
  })();
  lifetime.invalidate();
  const returned = lifetime.begin("same form");
  pending.reject(new Error("Old request failed"));
  await completion;
  assert.deepEqual(outcomes, []);
  assert.equal(lifetime.owns(returned, "same form"), true);
});

const props = { label: "Queue measure", pendingLabel: "Queueing…", successLabel: "Queued", retryLabel: "Retry queue" };
const render = extra => renderToStaticMarkup(React.createElement(AsyncActionButton, { ...props, ...extra }));

test("each state has one accessible label while all phases reserve the same button footprint", () => {
  for (const [status, label] of Object.entries({ idle: "Queue measure", pending: "Queueing…", success: "Queued", error: "Retry queue" })) {
    const html = render({ status });
    assert.match(html, new RegExp(`data-action-state="${status}"`));
    assert.equal((html.match(/aria-hidden="false"/g) ?? []).length, 1);
    assert.equal((html.match(/class="async-action-label(?: is-visible)?"/g) ?? []).length, 4);
    assert.ok(html.includes(`aria-hidden="false">${label}</span>`));
    assert.match(html, new RegExp(`aria-busy="${status === "pending"}"`));
    if (status === "pending" || status === "success") assert.match(html, /disabled=""/);
    else assert.doesNotMatch(html, /disabled=""/);
  }
  assert.doesNotMatch(render({ disabled: true }), /data-action-state="success"|lucide-check/);
});

