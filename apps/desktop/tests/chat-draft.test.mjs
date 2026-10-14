import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/useWorkspaceChatDraft.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { readChatDraft, saveChatDraft } = loaded.exports;
const storage = () => {
  const values = new Map();
  return { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
};

test("typed questions remain isolated across workspace switches and a reload", () => {
  const local = storage();
  assert.equal(saveChatDraft(local, "a", "Debug sales"), true);
  assert.equal(readChatDraft(local, "b").value, "");
  saveChatDraft(local, "b", "Explain returns");
  assert.equal(readChatDraft(local, "a").value, "Debug sales");
  assert.equal(readChatDraft(local, "b").value, "Explain returns");
  saveChatDraft(local, "a", "");
  assert.equal(readChatDraft(local, "a").value, "");
  assert.equal(readChatDraft(local, "b").value, "Explain returns");
});

test("blocked storage reports failure and oversized old drafts respect the request limit", () => {
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("quota"); }, removeItem() { throw new Error("blocked"); } };
  assert.deepEqual(readChatDraft(blocked, "a"), { value: "", failed: true });
  assert.equal(saveChatDraft(blocked, "a", "Question"), false);
  assert.equal(saveChatDraft(blocked, "a", ""), false);
  const local = storage();
  saveChatDraft(local, "a", "x".repeat(5000));
  assert.equal(readChatDraft(local, "a").value.length, 4000);
});
