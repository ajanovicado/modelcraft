import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/SegmentedControl.tsx", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { SegmentedControl } = loaded.exports;
const options = [{ value: "report", label: "Report" }, { value: "data", label: "Data" }, { value: "model", label: "Model" }, { value: "dax", label: "DAX Lab" }];
const tabs = (extra = {}) => ({ mode: "tabs", label: "Workspace views", options, value: "data", onChange() {}, tabIdPrefix: "workspace-tab-", panelIdPrefix: "workspace-panel-", ...extra });

test("workspace tabs retain stable labels, panel relationships and one tab stop", () => {
  const tree = SegmentedControl(tabs());
  const buttons = tree.props.children[1];
  assert.equal(tree.props.role, "tablist");
  assert.equal(tree.props["aria-label"], "Workspace views");
  assert.equal(tree.props.style["--segment-count"], 4);
  assert.equal(tree.props.style["--segment-index"], 1);
  assert.deepEqual(buttons.map(button => button.props.children), ["Report", "Data", "Model", "DAX Lab"]);
  assert.deepEqual(buttons.map(button => button.props.tabIndex), [-1, 0, -1, -1]);
  buttons.forEach((button, index) => {
    assert.equal(button.props.role, "tab");
    assert.equal(button.props.id, `workspace-tab-${options[index].value}`);
    assert.equal(button.props["aria-controls"], `workspace-panel-${options[index].value}`);
    assert.equal(button.props["aria-selected"], index === 1);
    assert.equal(button.props["aria-pressed"], undefined);
  });
  assert.equal(tree.props.children[0].props["aria-hidden"], "true");
});

test("tab keyboard navigation activates and focuses within its own control", () => {
  for (const [from, key, target] of [[0, "ArrowLeft", 3], [3, "ArrowRight", 0], [1, "ArrowRight", 2], [2, "ArrowLeft", 1], [2, "Home", 0], [1, "End", 3]]) {
    const calls = [];
    const tree = SegmentedControl(tabs({ onChange: value => calls.push(["change", value]) }));
    const button = tree.props.children[1][from];
    button.props.onKeyDown({ key, preventDefault: () => calls.push(["prevent"]), currentTarget: { parentElement: { querySelector: selector => {
      assert.equal(selector, `[data-segment-index="${target}"]`);
      return { focus: () => calls.push(["focus", target]) };
    } } } });
    assert.deepEqual(calls, [["prevent"], ["change", options[target].value], ["focus", target]]);
  }
});

test("tab navigation leaves unrelated keys and native activation alone", () => {
  const changes = [];
  const tree = SegmentedControl(tabs({ onChange: value => changes.push(value) }));
  const button = tree.props.children[1][2];
  for (const key of ["Tab", "ArrowDown", "Enter", " "]) button.props.onKeyDown({ key, preventDefault() { assert.fail("Native keys must not be consumed"); } });
  assert.deepEqual(changes, []);
  button.props.onClick();
  assert.deepEqual(changes, ["model"]);
});

test("history scopes stay native pressed buttons with all labels available", () => {
  const changes = [];
  const filters = [{ value: "all", label: "All chats" }, { value: "workspace", label: "Workspace" }, { value: "dax", label: "DAX" }];
  const props = { mode: "filter", label: "Filter chat history", options: filters, value: "workspace", onChange: value => changes.push(value) };
  const tree = SegmentedControl(props);
  const buttons = tree.props.children[1];
  assert.equal(tree.props.role, "group");
  assert.deepEqual(buttons.map(button => button.props["aria-pressed"]), [false, true, false]);
  buttons.forEach(button => {
    assert.equal(button.props.role, undefined);
    assert.equal(button.props.tabIndex, undefined);
    assert.equal(button.props.onKeyDown, undefined);
    assert.equal(button.props["aria-selected"], undefined);
  });
  buttons[2].props.onClick();
  assert.deepEqual(changes, ["dax"]);
  const html = renderToStaticMarkup(React.createElement(SegmentedControl, props));
  assert.match(html, /aria-pressed="false"[^>]*>All chats<\/button>/);
  assert.match(html, /aria-pressed="true"[^>]*>Workspace<\/button>/);
  assert.match(html, /aria-pressed="false"[^>]*>DAX<\/button>/);
});

test("selection updates indicator position without changing track labels or dimensions", () => {
  const first = SegmentedControl(tabs({ value: "report" }));
  const last = SegmentedControl(tabs({ value: "dax" }));
  assert.equal(first.props.style["--segment-index"], 0);
  assert.equal(last.props.style["--segment-index"], 3);
  assert.equal(first.props.style["--segment-count"], last.props.style["--segment-count"]);
  assert.deepEqual(first.props.children[1].map(button => button.props.children), last.props.children[1].map(button => button.props.children));
});
