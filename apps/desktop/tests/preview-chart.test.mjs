import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("../src/PreviewChart.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { paletteConfig } = loaded.exports;

test("a missing palette adds no config keys (Vega-Lite crashes on mark: undefined)", () => {
  assert.deepEqual(paletteConfig(undefined), {});
  assert.deepEqual(paletteConfig([]), {});
  assert.equal(Object.hasOwn(paletteConfig(undefined), "mark"), false);
});

test("a theme palette colors marks and categories", () => {
  assert.deepEqual(paletteConfig(["#404040", "#1F4E79"]), { range: { category: ["#404040", "#1F4E79"] }, mark: { color: "#404040" } });
});
