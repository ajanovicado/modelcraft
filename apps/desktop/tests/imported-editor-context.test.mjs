import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import test from "node:test";
import { build, transform } from "esbuild";
import ts from "typescript";

const require = createRequire(import.meta.url);
const React = require("react");
let current;
const same = (left, right) => Boolean(left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index])));

// Execute the editor's real hook/effect lifecycle without mounting its data-fetching children.
// The harness retains state and refs, performs effect cleanup before setup, and drains queued rerenders.
const hooks = {
  ...React,
  useState(initial) {
    const owner = current, index = owner.cursor++;
    if (!owner.slots[index]) owner.slots[index] = { value: typeof initial === "function" ? initial() : initial };
    return [owner.slots[index].value, next => {
      const value = typeof next === "function" ? next(owner.slots[index].value) : next;
      if (!Object.is(value, owner.slots[index].value)) { owner.slots[index].value = value; owner.dirty = true; }
    }];
  },
  useRef(initial) {
    const owner = current, index = owner.cursor++;
    if (!owner.slots[index]) owner.slots[index] = { current: initial };
    return owner.slots[index];
  },
  useMemo(compute, deps) {
    const owner = current, index = owner.cursor++;
    if (!same(owner.slots[index]?.deps, deps)) owner.slots[index] = { deps, value: compute() };
    return owner.slots[index].value;
  },
  useCallback(callback, deps) { return hooks.useMemo(() => callback, deps); },
  useEffect(effect, deps) {
    const owner = current, index = owner.cursor++;
    if (!same(owner.slots[index]?.deps, deps)) owner.effects.push({ index, effect, deps });
  },
};

const compiled = await build({
  entryPoints: [new URL("../src/ImportedEditor.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")],
  bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" }, logLevel: "silent",
  plugins: [{ name: "isolate-background-reads", setup(builder) {
    builder.onLoad({ filter: /[\\/]useRenderMeta\.ts$/ }, () => ({ contents: "export function useRenderMeta() { return {}; }", loader: "ts" }));
    builder.onLoad({ filter: /[\\/]TestsPane\.tsx$/ }, () => ({ contents: "const state = { tests: [] }; export const TestsPane = () => null; export const PinTestDialog = () => null; export function useReportTests() { return { state }; }", loader: "tsx" }));
  } }],
});
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(name => name === "react" ? hooks : require(name), loaded, loaded.exports);
const { ImportedEditor } = loaded.exports;
globalThis.document = { addEventListener() {}, removeEventListener() {} };

const visual = (id, title) => ({ id, title, x: 0, y: 0, width: 100, height: 100 });
const page = (id, title, visuals) => ({ id, title, width: 1280, height: 720, visuals });
const pages = [page("p1", "Executive overview", [visual("v1", "Revenue")]), page("p2", "Orders", [visual("v2", "Order count")])];
const report = (items = pages) => ({ id: "report", workspace_id: "workspace", name: "Report", pages: items, tables: [], measures: [] });
const find = (node, predicate) => {
  if (!React.isValidElement(node)) return undefined;
  if (predicate(node)) return node;
  for (const child of React.Children.toArray(node.props.children)) { const found = find(child, predicate); if (found) return found; }
  return undefined;
};

function editor(extra = {}) {
  const calls = [];
  const owner = { cursor: 0, slots: [], effects: [], dirty: true, tree: undefined, props: {
    report: report(), active: true, appBusy: false, pageId: "p1", onPage() {}, formatKey: "r1",
    onOpenDesktop: async () => {}, onDax() {}, onReportChanged() {}, onSelection: context => calls.push(context),
    editing: { client: { formatCatalog: () => new Promise(() => {}) }, head: { id: "r1" }, activeBusy: null, history: [], loadHistory() {}, submit: async () => ({ status: "applied" }) }, ...extra,
  } };
  const flush = () => {
    let loops = 0;
    while (owner.dirty) {
      assert.ok(loops++ < 20, "editor effects should settle");
      owner.dirty = false; owner.cursor = 0; owner.effects = []; current = owner;
      owner.tree = ImportedEditor(owner.props);
      for (const effect of owner.effects) owner.slots[effect.index]?.cleanup?.();
      for (const effect of owner.effects) owner.slots[effect.index] = { deps: effect.deps, cleanup: effect.effect() };
    }
  };
  const canvas = () => find(owner.tree, element => typeof element.props.onSelectVisual === "function");
  flush();
  return {
    calls, canvas,
    update(next) { Object.assign(owner.props, next); owner.dirty = true; flush(); },
    select(id) { canvas().props.onSelectVisual(id, { ctrlKey: false }); flush(); },
    unmount() { for (const slot of owner.slots) slot?.cleanup?.(); },
  };
}

test("the visible page is the default chat target before selecting a visual", () => {
  const view = editor();
  assert.deepEqual(view.calls, [{ page_id: "p1", label: "Executive overview" }]);
  view.unmount();
});

test("visual selection targets its page, and clearing selection returns to the visible page", () => {
  const view = editor();
  view.select("v1");
  assert.deepEqual(view.calls.at(-1), { page_id: "p1", visual_id: "v1", label: "Revenue" });
  view.select(null);
  assert.deepEqual(view.calls.at(-1), { page_id: "p1", label: "Executive overview" });
  view.unmount();
});

for (const reusedId of [false, true]) test(`changing pages never publishes the previous visual (reused ID: ${reusedId})`, () => {
  const items = [pages[0], page("p2", "Orders", [visual(reusedId ? "v1" : "v2", "Order count")])];
  const view = editor({ report: report(items) });
  view.select("v1");
  const start = view.calls.length;
  view.update({ pageId: "p2" });
  assert.deepEqual(view.calls.slice(start), [{ page_id: "p2", label: "Orders" }]);
  assert.equal(view.canvas().props.selectedVisualId, undefined);
  view.unmount();
});

test("inactive report view clears its automatic context; returning uses the current page", () => {
  const view = editor();
  view.select("v1");
  view.update({ active: false });
  assert.equal(view.calls.at(-1), undefined);
  view.update({ pageId: "p2" });
  assert.equal(view.calls.at(-1), undefined);
  view.update({ active: true });
  assert.deepEqual(view.calls.at(-1), { page_id: "p2", label: "Orders" });
  view.unmount();
});

test("unmount clears the automatic report target", () => {
  const view = editor();
  view.unmount();
  assert.equal(view.calls.at(-1), undefined);
});

test("an empty report publishes no page target", () => {
  const view = editor({ report: report([]) });
  assert.deepEqual(view.calls, [undefined]);
  view.unmount();
});

test("a deleted selected visual returns chat to the page rather than keeping a stale target", () => {
  const view = editor();
  view.select("v1");
  view.update({ report: report([page("p1", "Executive overview", [])]) });
  assert.deepEqual(view.calls.at(-1), { page_id: "p1", label: "Executive overview" });
  assert.equal(view.canvas().props.selectedVisualId, undefined);
  view.unmount();
});

test("page title changes update the default context label", () => {
  const view = editor();
  view.update({ report: report([page("p1", "Renamed overview", pages[0].visuals)]) });
  assert.deepEqual(view.calls.at(-1), { page_id: "p1", label: "Renamed overview" });
  view.unmount();
});

test("App preserves explicit attachments for automatic page context and retargets on visual selection", async () => {
  const text = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const source = ts.createSourceFile("App.tsx", text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  const visit = node => {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name) && node.name.text === "selectReportVisual") callback = node.initializer.arguments[0].getText(source);
    ts.forEachChild(node, visit);
  };
  visit(source);
  assert.ok(callback, "test executes the App's actual selection callback");
  const { code } = await transform(`const callback = ${callback};`, { loader: "ts" });
  const attached = { measure_id: "Total Sales", label: "Explicit measure" };
  let selection, explicit = attached;
  const select = new Function("setReportSelection", "setAttachedContext", `${code}; return callback;`)(value => { selection = value; }, value => { explicit = value; });
  select({ page_id: "p1", label: "Overview" });
  assert.deepEqual(selection, { page_id: "p1", label: "Overview" });
  assert.equal(explicit, attached);
  select(undefined);
  assert.equal(selection, undefined);
  assert.equal(explicit, attached);
  select({ page_id: "p1", visual_id: "v1", label: "Revenue" });
  assert.equal(selection.visual_id, "v1");
  assert.equal(explicit, undefined);
});
