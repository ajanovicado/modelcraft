import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/ModelDiagram.tsx", import.meta.url))], jsx: "automatic", bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);

test("a thousand-table model renders a bounded, navigable diagram and explorer", () => {
  const tables = Array.from({length:1000}, (_,i) => ({id:`t${i}`,name:`Table ${i}`,rowCount:1000000,columns:Array.from({length:200},(_,j) => ({id:`c${j}`,name:`Column ${j}`,dataType:"string"}))}));
  const relationships = tables.slice(1).map(t => ({id:`r${t.id}`,fromTableId:"t0",toTableId:t.id,fromColumnId:"c0",toColumnId:"c0",cardinality:"many-to-one",reviewStatus:"pending",blockers:[]}));
  const html = renderToStaticMarkup(createElement(loaded.exports.ModelDiagram, {tables,relationships,selectedIds:new Set(),onReview:()=>{}}));
  assert.equal((html.match(/class="mwb-canvas-node/g)||[]).length,12);
  assert.equal((html.match(/<button[^>]+aria-pressed=/g)||[]).length,50);
  assert.ok(html.includes("Search model tables") && html.includes("Next") && html.includes("12 of 1000 tables"));
  assert.ok(html.length < 100000, `Bounded initial DOM: ${html.length} bytes`);
});
