import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/ProductUI.tsx", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { RichText } = loaded.exports;
const render = text => renderToStaticMarkup(React.createElement(RichText, { text }));

test("technical answers preserve ordered steps and separate bullet lists", () => {
  const html = render("1. Check the type\n2. Fix the formula\n- Verify totals\n- Review context");
  assert.match(html, /<ol><li>Check the type<\/li><li>Fix the formula<\/li><\/ol>/);
  assert.match(html, /<ul><li>Verify totals/);
});

test("comparison tables render as accessible data tables with escaped model content", () => {
  const html = render("| Measure | Meaning |\n| --- | --- |\n| **Sales** | Adds amounts |\n| <script>run()</script> | `SUM` |");
  assert.match(html, /aria-label="Answer table"/);
  assert.match(html, /<th scope="col">Measure<\/th>/);
  assert.match(html, /<strong>Sales<\/strong>/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test("code is preserved exactly and supports copying without making it executable", () => {
  const html = render("```python\n  print('<script>')\n```\nExplain the result");
  assert.match(html, /aria-label="Copy code"/);
  assert.match(html, /<pre><code>  print/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /Continue in DAX Lab/);
});

test("incomplete table-like content remains readable during streaming", () => {
  const html = render("| Measure | Meaning |\n| --");
  assert.doesNotMatch(html, /<table>/);
  assert.match(html, /Measure \| Meaning/);
});
