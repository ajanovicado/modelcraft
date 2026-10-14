import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/ConversationHistory.tsx", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { ConversationHistory, filterConversationHistory, conversationTitle } = loaded.exports;
const item = (id, date, title = id) => ({ id, updatedAt: date, title, preview: "An answer", turnCount: 2 });

test("history searches full conversation content with multiple terms and sorts newest first", () => {
  const items = [item("old", "2026-09-01T10:00:00Z"), { ...item("middle", "2026-09-02T10:00:00Z"), searchText: "DAX filter context" }, { ...item("new", "2026-09-03T10:00:00Z"), searchText: "Filter context in dax" }];
  assert.deepEqual(filterConversationHistory(items, "  DAX  CONTEXT ").map(i => i.id), ["new", "middle"]);
  assert.deepEqual(filterConversationHistory(items, "").map(i => i.id), ["new", "middle", "old"]);
  assert.deepEqual(items.map(i => i.id), ["old", "middle", "new"]);
});

test("empty history explains how chats are kept without suggesting deletion", () => {
  const html = renderToStaticMarkup(React.createElement(ConversationHistory, { items: [], onSelect() {}, onClose() {} }));
  assert.match(html, /No previous chats yet/);
  assert.match(html, /aria-label="Search chats"/);
  assert.match(html, /aria-label="Back to current chat"/);
});

test("saved content is escaped and long history initially renders only twenty chats", () => {
  const items = Array.from({ length: 25 }, (_, i) => item(String(i), "2026-09-01T10:00:00Z", i === 0 ? "<script>run()</script>" : `Question ${i}`));
  const html = renderToStaticMarkup(React.createElement(ConversationHistory, { items, onSelect() {}, onClose() {} }));
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.equal((html.match(/conversation-history-row/g) ?? []).length, 20);
  assert.match(html, /Show more chats/);
});

test("titles use compact readable user text", () => {
  assert.equal(conversationTitle("  Explain\n\n filter\tcontext  "), "Explain filter context");
  assert.equal(conversationTitle(" "), "Untitled chat");
  assert.equal(conversationTitle("x".repeat(200)).length, 80);
});

test("shared history filters scope independently from full-content search", () => {
  const items = [{ ...item("workspace:1", "2026-09-02T10:00:00Z"), scope: "workspace", searchText: "Margin filter context" }, { ...item("dax:1", "2026-09-03T10:00:00Z"), scope: "dax", searchText: "Margin filter context" }, { ...item("workspace:current", "2026-09-04T10:00:00Z"), scope: "workspace", current: true }];
  assert.deepEqual(filterConversationHistory(items, "margin context", "workspace").map(i => i.id), ["workspace:1"]);
  assert.deepEqual(filterConversationHistory(items, "", "dax").map(i => i.id), ["dax:1"]);
  assert.deepEqual(filterConversationHistory(items, "margin context").map(i => i.id), ["dax:1", "workspace:1"]);
  const html = renderToStaticMarkup(React.createElement(ConversationHistory, { items, onSelect() {}, onClose() {} }));
  assert.match(html, /aria-label="Filter chat history"/);
  assert.match(html, /class="segmented-control conversation-history-filters" role="group"/);
  assert.match(html, /aria-pressed="true"[^>]*>All chats<\/button>/);
  assert.match(html, /aria-pressed="false"[^>]*>Workspace<\/button>/);
  assert.match(html, /aria-pressed="false"[^>]*>DAX<\/button>/);
  assert.match(html, />Workspace<\/span>/);
  assert.match(html, />DAX<\/span>/);
  assert.match(html, /conversation-history-current/);
  assert.match(html, /3 chats/);
  assert.doesNotMatch(html, /3 saved chats/);
});
