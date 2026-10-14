import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

// Bundle the pure partition helper with the same compiler the app uses.
const compiled = await build({ entryPoints: [new URL("../src/chatHistory.ts", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { partitionMessages, loadAllMessages, buildWorkspaceHistoryItems, workspaceDaxHandoff } = loaded.exports;

const at = (minute) => `2026-09-04 10:${String(minute).padStart(2, "0")}:00`;
const msg = (id, minute, role = "user") => ({ id, role, content: `m${id}`, citations: [], created_at: at(minute) });

test("without boundaries every stored message stays active", () => {
  const { active, archived } = partitionMessages([msg(1, 0), msg(2, 1)], [], "");
  assert.equal(active.length, 2);
  assert.deepEqual(archived, []);
});

test("a boundary hides prior messages into one archived segment", () => {
  const messages = [msg(1, 0), msg(2, 1, "assistant"), msg(3, 2), msg(4, 3, "assistant")];
  const { active, archived } = partitionMessages(messages, [2], "");
  assert.deepEqual(active.map((m) => m.id), [3, 4]);
  assert.equal(archived.length, 1);
  assert.deepEqual(archived[0].messages.map((m) => m.id), [1, 2]);
  assert.equal(archived[0].endedAt, at(1));
});

test("multiple clears produce ordered read-only segments", () => {
  const messages = [msg(1, 0), msg(2, 1), msg(3, 2), msg(4, 3), msg(5, 4)];
  const { active, archived } = partitionMessages(messages, [2, 4], "");
  assert.deepEqual(active.map((m) => m.id), [5]);
  assert.deepEqual(archived.map((s) => s.messages.map((m) => m.id)), [[1, 2], [3, 4]]);
});

test("previous data conversations remain readable without entering active context", () => {
  const messages = [msg(1, 0), msg(2, 1), msg(3, 30), msg(4, 31)];
  const { active, archived } = partitionMessages(messages, [3], "2026-09-04T10:15:00Z");
  assert.deepEqual(active.map((m) => m.id), [4]);
  assert.deepEqual(archived.map((s) => s.messages.map((m) => m.id)), [[1, 2], [3]]);
  assert.equal(archived[0].previousData, true);
  assert.equal(archived[1].previousData, undefined);
});

test("a data import archives the preceding conversation without an explicit new-chat boundary", () => {
  const { active, archived } = partitionMessages([msg(1, 0), msg(2, 1), msg(3, 30)], [], "2026-09-04T10:15:00Z");
  assert.deepEqual(active.map(m => m.id), [3]);
  assert.deepEqual(archived[0].messages.map(m => m.id), [1, 2]);
  assert.equal(archived[0].previousData, true);
});

test("second-precision engine messages are not hidden within the new snapshot's second", () => {
  const { active } = partitionMessages([msg(1, 1)], [], "2026-09-04T10:01:00.876Z");
  assert.deepEqual(active.map(m => m.id), [1]);
});

test("replayed or unordered boundary ids do not duplicate archived messages", () => {
  const { active, archived } = partitionMessages([msg(1, 0), msg(2, 1), msg(3, 2)], [2, 1, 2, -1], "");
  assert.deepEqual(active.map(m => m.id), [3]);
  assert.deepEqual(archived.flatMap(chat => chat.messages.map(m => m.id)), [1, 2]);
});

test("all history pages survive well beyond the latest-message window", async () => {
  const stored = Array.from({ length: 430 }, (_, index) => msg(index + 1, 0));
  const cursors = [];
  const result = await loadAllMessages(async after => { cursors.push(after); return stored.filter(m => m.id > after).slice(0, 200); });
  assert.equal(result.length, 430);
  assert.deepEqual(cursors, [0, 200, 400]);
  assert.equal(result[0].id, 1);
});

test("a stalled history cursor reports incomplete loading instead of looping or claiming complete history", async () => {
  const repeated = Array.from({ length: 200 }, (_, index) => msg(index + 1, 0));
  await assert.rejects(loadAllMessages(async () => repeated), /fully loaded/);
});

test("empty segments are skipped and optimistic messages stay active", () => {
  const optimistic = { role: "user", content: "unsent", citations: [], created_at: at(50) };
  const { active, archived } = partitionMessages([msg(1, 0), msg(2, 1), optimistic], [1, 2], "");
  assert.deepEqual(archived.map((s) => s.messages.map((m) => m.id)), [[1], [2]]);
  assert.deepEqual(active.map((m) => m.content), ["unsent"]);
});

test("unified workspace history uses stable scoped keys and retains previous-data metadata", () => {
  const current = [{ ...msg(9, 30), content: "Explain the current forecast" }];
  const saved = [{ endedAt: at(5), previousData: true, messages: [{ ...msg(1, 0), content: "Explain\nold data" }, { ...msg(2, 5, "assistant"), content: "Old fields remain readable" }] }];
  const rows = buildWorkspaceHistoryItems(current, saved);
  assert.equal(rows[0].id, "workspace:current");
  assert.equal(rows[0].scope, "workspace");
  assert.equal(rows[0].current, true);
  assert.equal(rows[0].sourceId, undefined);
  assert.equal(rows[1].id, "workspace:1");
  assert.equal(rows[1].sourceId, "1");
  assert.equal(rows[1].title, "Explain old data");
  assert.equal(rows[1].updatedAt, at(5));
  assert.equal(rows[1].note, "Previous data");
  assert.match(rows[1].searchText, /Old fields remain readable/);
  assert.equal(saved[0].messages[0].content, "Explain\nold data");
  assert.equal(buildWorkspaceHistoryItems([], [])[0].id, "workspace:current");
});

test("DAX handoff keeps the clicked response's originating request and assistant source id", () => {
  const first = { ...msg(1, 0), content: "Build a margin measure for the Sales table" };
  const response = { ...msg(2, 1, "assistant"), content: "```dax\nMargin = DIVIDE([Profit], [Sales])\n```" };
  const later = { ...msg(3, 2), content: "Now explain a different Cost measure" };
  const handoff = workspaceDaxHandoff("Margin = DIVIDE([Profit], [Sales])", response, [first, response, later], "1");
  assert.deepEqual(handoff, { expression: "Margin = DIVIDE([Profit], [Sales])", requestText: first.content, sourceMessageId: 2, sourceLabel: first.content, sourceArchiveId: "1" });
  assert.equal(response.content, "```dax\nMargin = DIVIDE([Profit], [Sales])\n```");
  assert.equal(workspaceDaxHandoff("SUM(Orders[Sales])", { ...response, id: undefined }, []).requestText, "");
});
