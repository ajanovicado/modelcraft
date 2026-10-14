import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { build } from "esbuild";

const compiled = await build({entryPoints:[fileURLToPath(new URL("../src/RelationshipInspector.tsx",import.meta.url))],bundle:true,platform:"node",format:"cjs",packages:"external",write:false,jsx:"automatic"});
const loaded={exports:{}};
new Function("require","module","exports",compiled.outputFiles[0].text)(createRequire(import.meta.url),loaded,loaded.exports);
const tables=[{id:"orders",name:"Orders",columns:[{id:"region_id",name:"Region ID"}]},{id:"regions",name:"Regions",columns:[{id:"id",name:"ID"}]}];
const relationship={id:"join",fromTableId:"orders",toTableId:"regions",fromColumnId:"region_id",toColumnId:"id",cardinality:"many-to-one",reviewStatus:"pending",blockers:[],evidence:["Keys inspected"],active:true};
const render = changes => renderToStaticMarkup(createElement(loaded.exports.RelationshipInspector,{tables,relationship:{...relationship,...changes},onReview:()=>{},onBack:()=>{}}));

test("relationship details use display field names and explain the correct filter direction",()=>{
  const html=render({});
  assert.match(html,/Region ID/);
  assert.match(html,/Filters on Regions flow to Orders/);
  assert.match(html,/Pending review/);
  assert.match(html,/Review is required before use/);
  assert.match(render({crossFilterDirection:"both",active:false}),/Filters travel in both directions/);
  assert.match(render({active:false}),/relationship is inactive/);
});

test("blockers take precedence and the inspector cannot approve a relationship",()=>{
  const html=render({reviewStatus:"approved",blockers:["12 orphan rows"]});
  assert.match(html,/>Blocked</);
  assert.match(html,/12 orphan rows/);
  assert.match(html,/Review relationship/);
  assert.doesNotMatch(html,/>Approved<|type="checkbox"/);
});
