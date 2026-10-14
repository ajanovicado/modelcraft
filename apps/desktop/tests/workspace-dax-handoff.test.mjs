import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/useWorkspaceDaxHandoff.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { createWorkspaceDaxHandoffSession, readWorkspaceDaxHandoff, saveWorkspaceDaxHandoff, validateWorkspaceDaxHandoff, workspaceDaxHandoffKey } = loaded.exports;

const handoff = (workspaceId = "workspace-1", snapshotId = "snapshot_1") => ({
  workspaceId, snapshotId, sourceView: "model",
  source: { expression: "Total Sales = SUM('Orders'[Sales])", requestText: "Create a total sales measure for the model.", sourceMessageId: 42, sourceArchiveId: "17", sourceLabel: "Create a total sales measure for the model." },
});
const memoryStorage = () => {
  const values = new Map();
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
};
const stored = value => JSON.stringify({ version: 1, handoff: value });

test("reload restores exact expression, request and source provenance in its original scope", () => {
  const storage = memoryStorage();
  const original = handoff();
  assert.equal(saveWorkspaceDaxHandoff(storage, original.workspaceId, original.snapshotId, original), true);
  assert.deepEqual(readWorkspaceDaxHandoff(storage, original.workspaceId, original.snapshotId), { handoff: original, failed: false });
  assert.equal(storage.values.size, 1);
  assert.deepEqual(JSON.parse(storage.getItem(workspaceDaxHandoffKey(original.workspaceId, original.snapshotId))), { version: 1, handoff: original });
  original.source.expression = "Changed outside the persisted handoff";
  assert.equal(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1").handoff.source.expression, "Total Sales = SUM('Orders'[Sales])");
});

test("workspaces and data snapshots keep separate active handoffs", () => {
  const storage = memoryStorage();
  for (const value of [handoff(), handoff("workspace-2"), handoff("workspace-1", "snapshot_2")]) {
    assert.equal(saveWorkspaceDaxHandoff(storage, value.workspaceId, value.snapshotId, value), true);
  }
  assert.equal(readWorkspaceDaxHandoff(storage, "workspace-2", "snapshot_1").handoff.workspaceId, "workspace-2");
  assert.equal(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_2").handoff.snapshotId, "snapshot_2");
  assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-3", "snapshot_1"), { failed: false });
  assert.equal(storage.values.size, 3);
});

test("detaching, New chat, or opening an imported measure removes only the active scoped metadata", () => {
  const storage = memoryStorage();
  for (const value of [handoff(), handoff("workspace-2"), handoff("workspace-1", "snapshot_2")]) saveWorkspaceDaxHandoff(storage, value.workspaceId, value.snapshotId, value);
  assert.equal(saveWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1", undefined), true);
  assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1"), { failed: false });
  assert.equal(storage.values.size, 2);
  assert.ok(readWorkspaceDaxHandoff(storage, "workspace-2", "snapshot_1").handoff);
  assert.ok(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_2").handoff);
});

test("scope mismatch is discarded without attaching another workspace's provenance", () => {
  const storage = memoryStorage();
  const currentKey = workspaceDaxHandoffKey("workspace-1", "snapshot_1");
  const otherKey = workspaceDaxHandoffKey("workspace-2", "snapshot_1");
  storage.setItem(currentKey, stored(handoff("workspace-2")));
  storage.setItem(otherKey, stored(handoff("workspace-2")));
  assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1"), { handoff: undefined, failed: false });
  assert.equal(storage.getItem(currentKey), null);
  assert.ok(storage.getItem(otherKey));
});

const invalidCases = [
  ["malformed JSON", "{"],
  ["unknown envelope version", JSON.stringify({ version: 2, handoff: handoff() })],
  ["unrecognized envelope field", JSON.stringify({ version: 1, handoff: handoff(), transcript: "do not retain" })],
  ["unrecognized provenance field", stored({ ...handoff(), transcript: "do not retain" })],
  ["unrecognized source field", stored({ ...handoff(), source: { ...handoff().source, hidden: "do not retain" } })],
  ["invalid source view", stored({ ...handoff(), sourceView: "dax" })],
  ["non-string source view", stored({ ...handoff(), sourceView: ["model"] })],
  ["empty expression", stored({ ...handoff(), source: { ...handoff().source, expression: " \n" } })],
  ["oversized expression", stored({ ...handoff(), source: { ...handoff().source, expression: "x".repeat(4001) } })],
  ["oversized origin request", stored({ ...handoff(), source: { ...handoff().source, requestText: "x".repeat(4001) } })],
  ["non-string request", stored({ ...handoff(), source: { ...handoff().source, requestText: 12 } })],
  ["oversized source label", stored({ ...handoff(), source: { ...handoff().source, sourceLabel: "x".repeat(81) } })],
  ["oversized archive id", stored({ ...handoff(), source: { ...handoff().source, sourceArchiveId: "x".repeat(201) } })],
  ["unsafe message id", stored({ ...handoff(), source: { ...handoff().source, sourceMessageId: Number.MAX_SAFE_INTEGER + 1 } })],
  ["negative message id", stored({ ...handoff(), source: { ...handoff().source, sourceMessageId: -1 } })],
  ["string message id", stored({ ...handoff(), source: { ...handoff().source, sourceMessageId: "42" } })],
  ["null character in source", stored({ ...handoff(), source: { ...handoff().source, expression: "SUM\0(Sales)" } })],
  ["oversized stored record", "x".repeat(20_001)],
];
for (const [name, raw] of invalidCases) {
  test(`${name} is discarded on reload`, () => {
    const storage = memoryStorage();
    const key = workspaceDaxHandoffKey("workspace-1", "snapshot_1");
    storage.setItem(key, raw);
    assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1"), { handoff: undefined, failed: false });
    assert.equal(storage.getItem(key), null);
  });
}

test("validation accepts optional source IDs and returns a separate approved object", () => {
  const value = handoff();
  delete value.source.sourceArchiveId;
  delete value.source.sourceMessageId;
  value.source.requestText = "";
  const approved = validateWorkspaceDaxHandoff(value, value.workspaceId, value.snapshotId);
  assert.deepEqual(approved, value);
  assert.notEqual(approved, value);
  assert.notEqual(approved.source, value.source);
  assert.equal(validateWorkspaceDaxHandoff({ ...value, sourceView: { toString: () => "model" } }, value.workspaceId, value.snapshotId), undefined);
});

test("invalid updates remove stale metadata instead of persisting a partially accepted object", () => {
  const storage = memoryStorage();
  saveWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1", handoff());
  assert.equal(saveWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1", handoff("workspace-2")), false);
  assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1"), { failed: false });
});

test("encoded write limits match reload limits so oversized escaped text is never claimed persisted", () => {
  const storage = memoryStorage();
  const value = handoff();
  value.source.expression = "x" + "\ud800".repeat(3999);
  value.source.requestText = "\ud800".repeat(4000);
  assert.ok(validateWorkspaceDaxHandoff(value, value.workspaceId, value.snapshotId));
  assert.equal(saveWorkspaceDaxHandoff(storage, value.workspaceId, value.snapshotId, value), false);
  assert.deepEqual(readWorkspaceDaxHandoff(storage, value.workspaceId, value.snapshotId), { failed: false });
});

test("invalid or ambiguous scope identifiers never access browser storage", () => {
  const storage = { getItem: () => { throw new Error("must not read"); }, setItem: () => { throw new Error("must not write"); }, removeItem: () => { throw new Error("must not delete"); } };
  for (const [workspace, snapshot] of [["", "s1"], ["w1:other", "s1"], ["w1", "s1:other"], ["w1", "x".repeat(201)]]) {
    assert.deepEqual(readWorkspaceDaxHandoff(storage, workspace, snapshot), { failed: false });
    assert.equal(saveWorkspaceDaxHandoff(storage, workspace, snapshot, undefined), false);
  }
});

test("blocked reads, quota failures, and failed detach are reported without a persistence claim", () => {
  const fail = () => { throw new Error("browser storage unavailable"); };
  assert.deepEqual(readWorkspaceDaxHandoff({ getItem: fail }, "workspace-1", "snapshot_1"), { failed: true });
  assert.deepEqual(readWorkspaceDaxHandoff({ getItem: () => "malformed", removeItem: fail }, "workspace-1", "snapshot_1"), { failed: true });
  assert.equal(saveWorkspaceDaxHandoff({ setItem: fail }, "workspace-1", "snapshot_1", handoff()), false);
  assert.equal(saveWorkspaceDaxHandoff({ removeItem: fail }, "workspace-1", "snapshot_1", undefined), false);
});

test("failed saves stay usable across workspace and snapshot switches in the open tab", () => {
  const storage = memoryStorage();
  storage.setItem = () => { throw new Error("quota exceeded"); };
  const session = createWorkspaceDaxHandoffSession(() => storage);
  session.set("workspace-1", "snapshot_1", handoff());
  assert.deepEqual(session.read("workspace-2", "snapshot_1"), { failed: false });
  assert.deepEqual(session.read("workspace-1", "snapshot_2"), { failed: false });
  assert.deepEqual(session.read("workspace-1", "snapshot_1"), { handoff: handoff(), failed: true });
  assert.deepEqual(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1"), { failed: false });
});

test("failed detach stays cleared when returning to its scope instead of resurrecting stored metadata", () => {
  const storage = memoryStorage();
  saveWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1", handoff());
  const session = createWorkspaceDaxHandoffSession(() => storage);
  assert.ok(session.read("workspace-1", "snapshot_1").handoff);
  storage.removeItem = () => { throw new Error("storage blocked"); };
  session.set("workspace-1", "snapshot_1", undefined);
  assert.deepEqual(session.read("workspace-2", "snapshot_1"), { failed: false });
  assert.deepEqual(session.read("workspace-1", "snapshot_1"), { handoff: undefined, failed: true });
  assert.ok(readWorkspaceDaxHandoff(storage, "workspace-1", "snapshot_1").handoff);
});

test("blocked storage access still keeps independent session provenance and detach state", () => {
  const session = createWorkspaceDaxHandoffSession(() => { throw new Error("localStorage denied"); });
  session.set("workspace-1", "snapshot_1", handoff());
  session.set("workspace-2", "snapshot_1", handoff("workspace-2"));
  session.set("workspace-1", "snapshot_1", undefined);
  assert.deepEqual(session.read("workspace-1", "snapshot_1"), { handoff: undefined, failed: true });
  assert.deepEqual(session.read("workspace-2", "snapshot_1"), { handoff: handoff("workspace-2"), failed: true });
});
