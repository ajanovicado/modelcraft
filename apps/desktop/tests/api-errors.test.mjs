import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/apiErrors.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { apiErrorMessage } = loaded.exports;

test("validation errors show messages without leaking submitted input or object dumps", () => {
  const error = apiErrorMessage({ detail: [{ msg: "Use a whole number", input: "private-question", loc: ["body", "length"] }] }, 422);
  assert.match(error, /Use a whole number/);
  assert.doesNotMatch(error, /private-question|object Object|body/);
});

test("missing or malformed errors provide actionable connection and workspace recovery", () => {
  assert.match(apiErrorMessage({ detail: {} }, 401), /Restart the app/);
  assert.match(apiErrorMessage(null, 409), /workspace changed/);
  assert.match(apiErrorMessage({ detail: [] }, 422), /Check your entries/);
  assert.match(apiErrorMessage({}, 503), /retry when it reconnects/);
  assert.equal(apiErrorMessage({ detail: "Select a date axis first." }, 422), "Select a date axis first.");
});
