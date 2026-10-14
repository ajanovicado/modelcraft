import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/editHistory.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { editHistory: reduce } = loaded.exports;
const initial = () => ({ past: [], present: [{ id: "page", title: "Overview", visuals: [{ id: "sales" }] }], future: [] });

test("undo restores removed pages and visuals, redo restores the edit, and a new edit branches", () => {
  const before = initial();
  const edited = reduce(before, { type: "edit", value: [{ id: "other", title: "New page", visuals: [] }] });
  const undone = reduce(edited, { type: "undo" });
  assert.deepEqual(undone.present, before.present);
  assert.deepEqual(reduce(undone, { type: "redo" }).present, edited.present);
  const branched = reduce(undone, { type: "edit", value: [{ id: "third" }] });
  assert.equal(branched.future.length, 0);
  assert.deepEqual(before.present[0].visuals, [{ id: "sales" }]);
});

test("typing in one field is one undo step; leaving the field starts a new step", () => {
  let state = initial();
  for (const title of ["A", "An", "Analysis"]) state = reduce(state, { type: "edit", group: "title", value: current => [{ ...current[0], title }] });
  assert.equal(state.past.length, 1);
  assert.equal(reduce(state, { type: "undo" }).present[0].title, "Overview");
  state = reduce(state, { type: "end-group" });
  state = reduce(state, { type: "edit", group: "title", value: current => [{ ...current[0], title: "Second session" }] });
  assert.equal(reduce(state, { type: "undo" }).present[0].title, "Analysis");
});

test("history stays bounded and new saved drafts cannot undo into stale bindings", () => {
  let state = initial();
  for (let i = 0; i < 150; i++) state = reduce(state, { type: "edit", value: [{ id: String(i) }] });
  assert.equal(state.past.length, 50);
  state = reduce(state, { type: "replace", value: initial().present });
  assert.equal(state.past.length, 0);
  assert.equal(state.future.length, 0);
  assert.equal(reduce(state, { type: "undo" }), state);
});
