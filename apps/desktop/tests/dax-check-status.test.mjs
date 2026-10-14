import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/daxCheckStatus.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { formulaNotes } = loaded.exports;
const formula = "```dax\nEVALUATE TOPN(2, 'T')\n```";
const references = "References supplied—not execution validation: [TOPN](https://learn.microsoft.com/dax/topn-function-dax)";

test("the check line under an imported formula is shown, never folded into the references", () => {
  const label = "Not validated: Power BI Desktop is closed and this formula needs it; names were checked.";
  const note = "This model has no field for 'category', so the query groups by 'T'[Name] instead. Check that this is what you meant.";
  const parts = formulaNotes(`${formula}\n\n${label}\n\n${note}\n\n${references}`);
  assert.deepEqual(parts.status, { text: label, tone: "warn" });
  assert.equal(parts.notes, note);
  assert.equal(parts.references, references);
});

test("validated and checked formulas read as ok, blank results as warnings", () => {
  assert.equal(formulaNotes(`${formula}\n\nValidated by Power BI Desktop.`).status.tone, "ok");
  assert.equal(formulaNotes(`${formula}\n\nChecked on the imported data (local preview).`).status.tone, "ok");
  const blank = "Ran on the imported data (local preview) but returned no value (blank or no rows): check its filter values before using it. 'DimChannel'[ChannelName] has no \"Stores\"; its values are Store, Online.";
  assert.deepEqual(formulaNotes(`${formula}\n\n${blank}`).status, { text: blank, tone: "warn" });
  assert.equal(formulaNotes(`${formula}\n\nRan in Power BI Desktop but returned no value (blank or no rows): check its filter values before using it.`).status.tone, "warn");
});

test("a managed formula keeps only its references, with no check line", () => {
  const parts = formulaNotes(`${formula}\n\n${references}`);
  assert.equal(parts.status, undefined);
  assert.equal(parts.notes, "");
  assert.equal(parts.references, references);
  assert.deepEqual(formulaNotes(formula), { status: undefined, notes: "", references: "" });
});

test("the DAX Lab renders the check line outside the folded references", () => {
  const source = readFileSync(new URL("../src/DaxLab.tsx", import.meta.url), "utf8");
  assert.match(source, /formulaNotes\(turn\.response\)/);
  assert.doesNotMatch(source, /<summary>Notes &amp; references<\/summary><RichText text=\{formulaRemainder/);
});
