import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/workspaceChatStorage.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { clearWorkspaceChatStorage } = loaded.exports;

test("deleting a workspace clears every snapshot's chat cache without touching similar workspace ids", () => {
  const values = new Map([
    ["studio.chatDraft.w1", "question"], ["studio.dax.w1", "formula"],
    ["studio.dax.conversation.v1:w1:s1", "history"], ["studio.dax.archive.v1:w1:s2", "archive"],
    ["studio.dax.composer.v1:w1:s1", "draft"], ["studio.dax.pending.v1:w1:s1", "retry"],
    ["studio.dax.handoff.v1:w1:s1", "source"], ["studio.dax.handoff.v1:w1:s2", "older source"],
    ["studio.chatRecovery.v1:w1:s1", "retry"], ["studio.lastWorkspace", "w1"],
    ["studio.chatDraft.w10", "keep"], ["studio.dax.pending.v1:w10:s1", "keep"], ["studio.dax.handoff.v1:w10:s1", "keep"], ["studio.navCollapsed", "true"],
  ]);
  const storage = { get length() { return values.size; }, key: index => [...values.keys()][index] ?? null, getItem: key => values.get(key) ?? null, removeItem: key => values.delete(key) };
  assert.equal(clearWorkspaceChatStorage(storage, "w1"), true);
  assert.deepEqual([...values.keys()], ["studio.chatDraft.w10", "studio.dax.pending.v1:w10:s1", "studio.dax.handoff.v1:w10:s1", "studio.navCollapsed"]);
});

test("cache cleanup failures are explicit", () => {
  assert.equal(clearWorkspaceChatStorage({ get length() { throw new Error("blocked"); } }, "w1"), false);
});
