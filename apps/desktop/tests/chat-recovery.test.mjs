import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/chatRecovery.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { readChatAttempt, saveChatAttempt, clearChatAttempt } = loaded.exports;
const storage = () => {
  const values = new Map();
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
};
const attempt = { workspaceId: "w1", snapshotId: "s1", boundaryId: 0, message: "Explain this chart", context: { page_id: "overview", visual_id: "sales", label: "Overview › Sales" }, baseHash: "a".repeat(64), key: "request-original-01" };
const receiptKey = "studio.chatRecovery.v1:w1:s1";

test("interrupted requests recover their immutable context and key after reload, isolated by data and workspace", () => {
  const local = storage();
  assert.equal(saveChatAttempt(local, attempt), true);
  assert.deepEqual(readChatAttempt(local, "w1", "s1"), attempt);
  assert.equal(readChatAttempt(local, "w2", "s1"), undefined);
  assert.equal(readChatAttempt(local, "w1", "s2"), undefined);
  clearChatAttempt(local, "w1", "s1");
  assert.equal(readChatAttempt(local, "w1", "s1"), undefined);
});

test("whole-workspace retries preserve explicit null context", () => {
  const local = storage();
  saveChatAttempt(local, { ...attempt, context: null });
  assert.equal(readChatAttempt(local, "w1", "s1").context, null);
});

test("malformed saved data never becomes a request", () => {
  for (const replacement of [{ message: "x".repeat(4001) }, { baseHash: "bad" }, { key: "bad" }, { context: { label: "Sales", visual_id: 12 } }, { context: { label: "Sales", visual_id: "x".repeat(121) } }, { context: { label: " ", visual_id: "sales" } }, { boundaryId: -1 }, { boundaryId: "0" }, { privateExtra: "unknown" }]) {
    const local = storage();
    assert.equal(saveChatAttempt(local, { ...attempt, ...replacement }), false);
    local.setItem(receiptKey, JSON.stringify({ ...attempt, ...replacement }));
    assert.equal(readChatAttempt(local, "w1", "s1"), undefined);
    assert.equal(local.getItem(receiptKey), null);
  }
});

test("New chat boundary prevents a leftover receipt from reviving an archived request", () => {
  const local = storage();
  saveChatAttempt(local, { ...attempt, boundaryId: 12 });
  assert.equal(readChatAttempt(local, "w1", "s1", 12).boundaryId, 12);
  assert.equal(readChatAttempt(local, "w1", "s1", 24), undefined);
  assert.equal(local.getItem(receiptKey), null);
});

test("legacy recovery is allowed only before the first New chat boundary", () => {
  const local = storage();
  const { boundaryId: _boundary, ...legacy } = attempt;
  local.setItem(receiptKey, JSON.stringify(legacy));
  assert.deepEqual(readChatAttempt(local, "w1", "s1", 0), legacy);
  assert.equal(readChatAttempt(local, "w1", "s1", 1), undefined);
  assert.equal(saveChatAttempt(local, legacy), false);
});

test("server completion outranks local pending data even if receipt cleanup is blocked", () => {
  const local = storage();
  saveChatAttempt(local, attempt);
  local.removeItem = () => { throw new Error("blocked"); };
  const messages = [{ role: "assistant", request_id: attempt.key, status: "completed" }];
  assert.equal(readChatAttempt(local, "w1", "s1", 0, messages), undefined);
  assert.equal(clearChatAttempt(local, "w1", "s1"), false);
  assert.ok(local.getItem(receiptKey));
});

test("unrelated or incomplete server turns preserve exact-envelope recovery", () => {
  const local = storage();
  saveChatAttempt(local, attempt);
  for (const messages of [[{ request_id: "other-request", status: "completed" }], [{ request_id: attempt.key, status: "interrupted" }], [{ request_id: attempt.key, status: "cancelled" }], [{ request_id: attempt.key, status: "failed" }], [{ request_id: attempt.key }]]) {
    assert.deepEqual(readChatAttempt(local, "w1", "s1", 0, messages), attempt);
  }
});

test("malformed, oversized and scope-mismatched records are discarded instead of retained indefinitely", () => {
  for (const raw of ["{", "x".repeat(32_001), JSON.stringify({ ...attempt, workspaceId: "wrong" }), JSON.stringify({ ...attempt, snapshotId: "wrong" }), JSON.stringify([attempt])]) {
    const local = storage();
    local.setItem(receiptKey, raw);
    assert.equal(readChatAttempt(local, "w1", "s1"), undefined);
    assert.equal(local.getItem(receiptKey), null);
  }
});

test("recovery scope identifiers cannot overlap other storage scopes", () => {
  const never = () => assert.fail("must not access storage");
  const local = { getItem: never, setItem: never, removeItem: never };
  assert.equal(readChatAttempt(local, "w1:other", "s1"), undefined);
  assert.equal(readChatAttempt(local, "w1", "s1:other"), undefined);
  assert.equal(clearChatAttempt(local, "w1:other", "s1"), false);
  assert.equal(saveChatAttempt(local, { ...attempt, workspaceId: "w1:other" }), false);
});

test("no-data recovery stays distinct from a snapshot, and successful cleanup is explicit", () => {
  const local = storage();
  const { snapshotId: _snapshot, ...withoutData } = attempt;
  saveChatAttempt(local, withoutData);
  assert.deepEqual(readChatAttempt(local, "w1"), { ...withoutData, snapshotId: undefined });
  assert.equal(readChatAttempt(local, "w1", "s1"), undefined);
  assert.equal(clearChatAttempt(local, "w1"), true);
  assert.equal(readChatAttempt(local, "w1"), undefined);
});
