import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

// Use the existing frontend compiler, in memory. No test pages or mock data are
// bundled into the app. These are render-contract tests; interaction QA is separate.
const compiled = await build({ entryPoints: [new URL("../src/ChatConversation.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { ChatConversation, ResponseActivity, readableReferences } = loaded.exports;

test("older drafts show table and field names, not profile IDs or model indexes", () => {
  const snapshot = { tables: [
    { table: { id: "superstore_orders_1_641f", name: "Orders", columns: [{ id: "category", name: "Category" }] } },
    { table: { row_count: 4 } },
  ] };
  assert.equal(
    readableReferences("Which [superstore_orders_1_641f.category] leads [superstore_orders_1_641f] [1]? Keep [unknown_id].", snapshot),
    "Which Orders[Category] leads Orders? Keep [unknown_id].",
  );
});
const noop = () => {};
const props = {
  snapshot: { tables: [{ table: { row_count: 12 } }, { table: { row_count: 4 } }] },
  draft: { status: "draft", pages: [{}, {}], understanding: { suggested_questions: ["Which fields are available?"] } },
  messages: [], streamed: "", streaming: false, active: true, busy: false, input: "",
  onInputChange: noop, onSend: noop, onStop: noop, onData: noop, onReviewDraft: noop, onRevise: noop, composerRef: { current: null },
};
const render = (overrides = {}) => renderToStaticMarkup(React.createElement(ChatConversation, { ...props, ...overrides }));
const question = { role: "user", content: "Help me explore sales", citations: [], created_at: "2026-09-04T12:00:00Z" };
const recovery = { recoveryRequest: { message: "Add a forecast", contextLabel: "Overview › Sales over time" }, onRecover: noop };

test("completed assistant DAX blocks expose a review handoff without executing code", () => {
  const content = "Try this:\n```dax\nSUM('Orders'[Sales])\n```\nRun it to check the result.";
  const html = render({ messages: [{ ...question, role: "assistant", content }], onUseDax: noop });
  assert.match(html, /Continue in DAX Lab/);
  assert.match(html, /<pre><code>SUM/);
  assert.doesNotMatch(render({ messages: [{ ...question, content }], onUseDax: noop }), /Continue in DAX Lab/);
  assert.doesNotMatch(render({ streamed: content, streaming: true, onUseDax: noop }), /Continue in DAX Lab/);
});

test("incomplete DAX fences and non-DAX code cannot be imported as formulas", () => {
  for (const content of ["```dax\nSUM('Orders'[Sales])", "```sql\nSELECT * FROM orders\n```", "```\nSUM('Orders'[Sales])\n``` "]) {
    assert.doesNotMatch(render({ messages: [{ ...question, role: "assistant", content }], onUseDax: noop }), /Continue in DAX Lab/);
  }
});

test("persisted incomplete assistant replies remain readable and copyable without a DAX handoff", () => {
  const content = "```dax\nSUM('Orders'[Sales])\n```";
  for (const status of ["interrupted", "cancelled", "failed"]) {
    const html = render({ messages: [{ ...question, role: "assistant", content, status, request_id: "request-partial" }], onUseDax: noop });
    assert.doesNotMatch(html, /Continue in DAX Lab/);
    assert.match(html, /before completion/);
    assert.match(html, /Copy response/);
    assert.match(html, /<pre><code>SUM/);
  }
  assert.match(render({ messages: [{ ...question, role: "assistant", content, status: "completed" }], onUseDax: noop }), /Continue in DAX Lab/);
});

test("discussion is explicitly distinguished from an applied draft change", () => {
  assert.match(render({ discussionOnly: true }), /No draft or Power BI changes were applied/);
  assert.doesNotMatch(render({ discussionOnly: true, streaming: true }), /Explanation only/);
});

test("pre-token activity is accessible, with three decorative pulse dots", () => {
  const html = renderToStaticMarkup(React.createElement(ResponseActivity));
  assert.match(html, /role="status"/);
  assert.match(html, /Preparing a response/);
  assert.match(html, /aria-hidden="true"><span><\/span><span><\/span><span><\/span>/);
  assert.doesNotMatch(html, /Thinking|chain.of.thought/);
});
test("a live request has visible activity before its first token", () => {
  const html = render({ messages: [question], streaming: true, busy: true });
  assert.match(html, /Preparing a response/);
  assert.match(html, /aria-label="Stop response"/);
  assert.doesNotMatch(html, /aria-label="Send message"/);
  assert.doesNotMatch(html, /<textarea[^>]*disabled/);
});
test("partial output replaces the waiting row and reports responding", () => {
  const html = render({ messages: [question], streaming: true, streamed: "Sales are available.", busy: true });
  assert.match(html, /Sales are available/);
  assert.match(html, /Responding/);
  assert.doesNotMatch(html, /Preparing a response|Copy response/);
});
test("stopped partial output remains visible without a stale animation", () => {
  const html = render({ ...recovery, messages: [question], streamed: "Partial answer", outcome: "stopped" });
  assert.match(html, /Partial answer/);
  assert.match(html, /Response stopped/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /response-dots|Stop response/);
});
test("interrupted output has recovery and no perpetual activity", () => {
  const html = render({ ...recovery, messages: [question], outcome: "interrupted" });
  assert.match(html, /Response interrupted/);
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /response-dots/);
});

test("the failure reason stays next to retry in the conversation", () => {
  const failureMessage = "Local AI is already handling a request. Wait for it to finish and retry.";
  const html = render({ ...recovery, messages: [question], outcome: "interrupted", failureMessage });
  assert.ok(html.includes(failureMessage));
  assert.match(html, /Retry/);
  assert.doesNotMatch(html, /Response interrupted|response-dots/);
  assert.doesNotMatch(render({ outcome: "stopped", failureMessage }), /already handling/);
  assert.doesNotMatch(render({ streaming: true, outcome: "interrupted", failureMessage }), /already handling/);
});
test("recovery identifies the attempted request even when it was never stored", () => {
  const html = render({ ...recovery, messages: [], outcome: "interrupted" });
  assert.match(html, /Add a forecast/);
  assert.match(html, /Retry target: Overview › Sales over time/);
  assert.match(html, /Retry<\/button>/);
  assert.doesNotMatch(render({ messages: [question], outcome: "interrupted" }), /Retry<\/button>/);
});

test("unsaved edits explain why sending is blocked and preserve the composer", () => {
  const html = render({ busy: true, input: "Keep my next question", blockedReason: "Save your report edits before sending a message.", onReviewEdits: noop });
  assert.match(html, /Save your report edits before sending a message/);
  assert.match(html, /Review report edits/);
  assert.match(html, /chat-input-hint chat-blocked-reason/);
  assert.match(html, /<textarea[^>]*>Keep my next question<\/textarea>/);
  assert.doesNotMatch(html, /<textarea[^>]*disabled/);
  assert.match(html, /disabled=""[^>]*aria-label="Send message"/);
});
test("idle chat has no workflow rail or fake activity", () => {
  const html = render({ messages: [question] });
  assert.doesNotMatch(html, /Dashboard workflow|workflow-rail|response-dots|What would you like to explore/);
  assert.match(html, /chat-compose-dock/);
  assert.match(html, /Workspace conversation/);
  assert.doesNotMatch(html, /Revise dashboard/);
  assert.match(html, /Review draft/);
});
test("empty conversation offers real-data questions", () => {
  const html = render();
  assert.match(html, /What would you like to explore/);
  assert.match(html, /Which fields are available/);
  assert.match(html, /2 tables/);
  assert.match(html, /16 rows/);
});
test("attached context is visible and removable without another prompt box", () => {
  const html = render({ context: { page_id: "detail", visual_id: "sales-region", label: "Detail › Sales by Region" }, onClearContext: noop });
  assert.match(html, /Detail › Sales by Region/);
  assert.match(html, /Remove attached context/);
  assert.equal((html.match(/<textarea/g) || []).length, 1);
});
test("approved draft is a compact review link, not a write action", () => {
  const html = render({ draft: { ...props.draft, status: "approved" } });
  assert.match(html, /Approved draft/);
  assert.doesNotMatch(html, /Review &amp; build|Approve model/);
});
test("long histories are bounded with an explicit load-earlier control", () => {
  const messages = Array.from({ length: 45 }, (_, i) => ({ ...question, content: `message-${i}-body`, created_at: String(i) }));
  const html = render({ messages });
  assert.match(html, /Load earlier messages \(5\)/);
  assert.doesNotMatch(html, /message-0-body/);
  assert.match(html, /message-44-body/);
});
test("completed assistant responses expose copy, not request activity", () => {
  const html = render({ messages: [{ ...question, role: "assistant", content: "A reviewed answer." }] });
  assert.match(html, /Copy response/);
  assert.doesNotMatch(html, /response-dots/);
});
test("new chat and history controls are explicit buttons", () => {
  const archived = [{ endedAt: "2026-09-04 10:00:00", messages: [{ ...question, id: 1 }] }];
  const html = render({ messages: [question], canClear: true, onClear: noop, archived });
  assert.match(html, /New chat/);
  assert.match(html, /aria-label="Chat history"/);
  assert.doesNotMatch(html, /Previous conversations/); // list stays closed until requested
});

test("history remains discoverable before the first chat is archived", () => {
  const html = render({ canClear: true, onClear: noop });
  assert.match(html, /aria-label="Chat history"/);
  assert.match(html, /disabled=""[^>]*title="Keep this conversation in history and start a new chat"/);
  assert.doesNotMatch(html, /Confirm new chat|Confirm<\/button>/);
});

test("unavailable local AI explains the blocked send while leaving the question editable", () => {
  const html = render({ blockedReason: "Local AI is not ready yet. Your question is kept while it starts.", input: "Explain CALCULATE" });
  assert.match(html, /Local AI is not ready yet/);
  assert.match(html, /disabled=""[^>]*aria-label="Send message"/);
  assert.doesNotMatch(html, /<textarea[^>]*disabled/);
});
test("clear control hides when the engine lacks the feature", () => {
  const html = render({ messages: [question] });
  assert.doesNotMatch(html, /New chat/);
});
test("user turns offer edit-and-resend, last assistant turn offers try-again", () => {
  const messages = [
    { ...question, id: 1 },
    { ...question, id: 2, role: "assistant", content: "Answer one." },
    { ...question, id: 3, content: "Second question" },
    { ...question, id: 4, role: "assistant", content: "Answer two." },
  ];
  const html = render({ messages, onEditMessage: noop, onRetry: noop });
  assert.equal((html.match(/Edit &amp; resend/g) || []).length, 2);
  assert.equal((html.match(/Try again/g) || []).length, 1);
  assert.match(html, /<time[^>]*dateTime="2026-09-04T12:00:00Z"/);
});

test("an unanswered newer request cannot replace the prior assistant's retry question", () => {
  const messages = [{ ...question, id: 1, content: "First original question" }, { ...question, id: 2, role: "assistant", content: "First answer" }, { ...question, id: 3, content: "Newer unanswered question" }];
  const html = render({ messages, onRetry: noop });
  assert.match(html, /title="Ask this again: First original question"/);
  assert.doesNotMatch(html, /title="Ask this again: Newer unanswered question"/);
  assert.doesNotMatch(render({ messages, onRetry: noop, busy: true }), /Try again/);
  assert.doesNotMatch(render({ messages, onRetry: noop, streaming: true }), /Try again/);
  assert.doesNotMatch(render({ messages, onRetry: noop, blockedReason: "The local engine is disconnected." }), /Try again/);
});

test("recovery respects the same engine readiness guard as the composer", () => {
  const html = render({ ...recovery, outcome: "interrupted", blockedReason: "The local engine is disconnected." });
  assert.match(html, /disabled="" title="The local engine is disconnected\."/);
  assert.match(html, /Retry/);
  assert.match(html, /Add a forecast/);
});
test("composer advertises the request character limit only near it", () => {
  assert.doesNotMatch(render({ input: "short" }), /\/4000/);
  assert.match(render({ input: "x".repeat(3300) }), /3300\/4000/);
});
test("planning activity is shown as the actual operation", () => {
  const html = render({ activity: "Revising the governed draft", busy: true });
  assert.match(html, /Revising the governed draft/);
  assert.doesNotMatch(html, /Stop response/);
});
test("motion has a reduced-motion fallback and transcript owns scrolling", async () => {
  const css = await readFile(new URL("../src/chat.css", import.meta.url), "utf8");
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.match(css, /animation: none/);
  assert.match(css, /\.chat-thread-scroll[^}]+overflow-y: auto/);
  assert.match(css, /\.chat-compose-dock[^}]+flex: 0 0 auto/);
});
