import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({entryPoints:[fileURLToPath(new URL("../src/modelLayout.ts",import.meta.url))],bundle:true,platform:"node",format:"cjs",write:false});
const loaded={exports:{}};
new Function("require","module","exports",compiled.outputFiles[0].text)(createRequire(import.meta.url),loaded,loaded.exports);
const {arrangeModel,MODEL_NODE_WIDTH}=loaded.exports;

function checkLayout(tables,relationships,heights,width,height) {
  const layout=arrangeModel(tables,relationships,heights,width,height);
  assert.equal(layout.positions.size,tables.length);
  const boxes=tables.map(t=>({...layout.positions.get(t.id),width:MODEL_NODE_WIDTH,height:heights.get(t.id)}));
  for(const a of boxes) {
    assert.ok(a.x>=0 && a.y>=0 && a.x+a.width<=layout.width && a.y+a.height<=layout.height);
    for(const b of boxes) if(a!==b) assert.ok(a.x+a.width<=b.x || b.x+b.width<=a.x || a.y+a.height<=b.y || b.y+b.height<=a.y,"Table cards must not overlap, including expanded cards");
  }
  return layout;
}

test("the fact table stays centered with dimensions on both sides of a wide viewport",()=>{
  const tables=[{id:"orders",role:"fact"},{id:"returns"},{id:"people"}];
  const relationships=tables.slice(1).map(t=>({fromTableId:"orders",toTableId:t.id}));
  const heights=new Map([["orders",262],["returns",122],["people",122]]);
  const layout=checkLayout(tables,relationships,heights,1000,850);
  const hub=layout.positions.get("orders");
  assert.equal(hub.x+MODEL_NODE_WIDTH/2,500);
  assert.ok(layout.positions.get("returns").x < hub.x && layout.positions.get("people").x > hub.x);
  assert.ok(hub.y>100,"The diagram uses the available height instead of sticking to the top");
});

test("narrow, expanded, dense and disconnected models remain bounded and non-overlapping",()=>{
  const tables=Array.from({length:12},(_,i)=>({id:`t${i}`,role:i===0?"fact":"dimension"}));
  const relationships=tables.slice(1).map(t=>({fromTableId:"t0",toTableId:t.id}));
  const heights=new Map(tables.map((t,i)=>[t.id,i===0?920:122+i*9]));
  for(const width of [350,640,919,920,1400]) for(const joins of [relationships,[]]) checkLayout(tables,joins,heights,width,500);
});
