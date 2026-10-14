import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

async function load(file) {
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../src/${file}`, import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" }, logLevel: "silent" });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
}
const save = await load("daxMeasureSave.ts");
const { renderToStaticMarkup } = await import("react-dom/server");
const React = await import("react");
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));
const queueButton = html => html.match(/<button\b[^>]*>[\s\S]*?<\/button>/g)?.find(button => button.includes('aria-hidden="false">Queue as a measure</span>')) ?? "";

test("a formula pasted with its own name line is split into the name and the formula", () => {
  assert.deepEqual(save.splitMeasureDefinition("Profit Margin % = DIVIDE([Profit],[Sales])"), { name: "Profit Margin %", formula: "DIVIDE([Profit],[Sales])" });
  assert.deepEqual(save.splitMeasureDefinition("Total :=\n  SUM(Sales[Amount])"), { name: "Total", formula: "SUM(Sales[Amount])" });
  // No definition: the formula is kept as typed, apart from the outer spaces.
  assert.deepEqual(save.splitMeasureDefinition("  SUM('Orders'[Sales]) "), { formula: "SUM('Orders'[Sales])" });
  assert.deepEqual(save.splitMeasureDefinition("IF(A = B, 1, 0)"), { formula: "IF(A = B, 1, 0)" });
  // A name with nothing after it is not a definition.
  assert.deepEqual(save.splitMeasureDefinition("Total ="), { formula: "Total =" });
  assert.equal(save.measureHeaderName("Profit Margin % = DIVIDE(1,2)"), "Profit Margin %");
  assert.equal(save.measureHeaderName("SUM(Sales[Amount])"), undefined);
});

test("a formula that starts with a variable is never split at the variable's equals sign", () => {
  const multi = "VAR total = SUM(Sales[Amount])\nRETURN total / 2";
  assert.deepEqual(save.splitMeasureDefinition(multi), { formula: multi });
  assert.deepEqual(save.splitMeasureDefinition("VAR x = 1 RETURN x"), { formula: "VAR x = 1 RETURN x" });
  assert.deepEqual(save.splitMeasureDefinition("  var x = 1\nRETURN x "), { formula: "var x = 1\nRETURN x" });
  assert.equal(save.measureHeaderName(multi), undefined);
  assert.equal(save.measureHeaderName("DEFINE VAR x = 1"), undefined);
  // A definition whose formula uses variables is still split at its own name.
  assert.deepEqual(save.splitMeasureDefinition("Half = VAR total = SUM(Sales[Amount])\nRETURN total / 2"), { name: "Half", formula: "VAR total = SUM(Sales[Amount])\nRETURN total / 2" });
  assert.deepEqual(save.splitMeasureDefinition("Return Rate = DIVIDE([Returns],[Sales])"), { name: "Return Rate", formula: "DIVIDE([Returns],[Sales])" });
});

test("the tables offered for a new measure: measure tables first, each once, no automatic date tables", () => {
  const tables = [{ TableName: "Sales" }, { TableName: "Sales" }, { TableName: "Agents" }, { TableName: "LocalDateTable_1a2b" }, { TableName: "DateTableTemplate_9" }, { TableName: "# Measures" }];
  const measures = [{ TableName: "# Measures" }, { TableName: "# Measures" }, { TableName: "Sales" }];
  assert.deepEqual(save.measureHomeTables(tables, measures), ["# Measures", "Sales", "Agents"]);
  assert.deepEqual(save.measureHomeTables([], []), []);
  assert.deepEqual(save.measureHomeTables([{ TableName: "" }], []), []);
  assert.equal(save.queuedMeasureNote("Margin"), "Queued “Margin”. Apply it from Pending Power BI changes.");
});

test("Save as a measure for an imported report offers a name, a table and a number format", async () => {
  const { SaveImportedMeasure } = await load("SaveImportedMeasure.tsx");
  const html = render(SaveImportedMeasure, { tables: ["# Measures", "Sales"], existing: ["Total"], expression: "Margin = DIVIDE([Profit],[Sales])", suggestedName: "Margin", disabled: false, onSave: async () => undefined });
  assert.ok(html.includes("Save as a measure") && html.includes("Queue as a measure"));
  assert.match(html, /aria-label="New measure name"[^>]*value="Margin"|value="Margin"[^>]*aria-label="New measure name"/);
  assert.match(html, /aria-label="Table for the new measure"/);
  assert.ok(html.includes("<option value=\"# Measures\"") && html.includes("<option value=\"Sales\""));
  assert.match(html, /aria-label="Number format of the new measure"/);
  // The name is free and the formula is there, so the button is enabled.
  assert.ok(queueButton(html));
  assert.doesNotMatch(queueButton(html), /<button[^>]*disabled=""/);
  const taken = render(SaveImportedMeasure, { tables: ["Sales"], existing: ["Margin"], expression: "1+1", suggestedName: "margin", disabled: false, onSave: async () => undefined });
  assert.ok(taken.includes("A measure named “margin” already exists."));
  assert.match(queueButton(taken), /<button[^>]*disabled=""/);
  const none = render(SaveImportedMeasure, { tables: [], existing: [], expression: "1+1", suggestedName: "X", disabled: false, onSave: async () => undefined });
  assert.ok(none.includes("This report has no table to add a measure to."));
  const off = render(SaveImportedMeasure, { tables: ["Sales"], existing: [], expression: "1+1", suggestedName: "X", disabled: true, onSave: async () => undefined });
  assert.match(queueButton(off), /<button[^>]*disabled=""/);
});

test("DAX Lab shows the save controls for an imported report, and keeps the draft-only ones otherwise", async () => {
  const stored = new Map([["studio.dax.w", "Margin = DIVIDE([Profit],[Sales])"]]);
  globalThis.localStorage = { getItem: key => stored.get(key) ?? null, setItem: (key, value) => { stored.set(key, String(value)); }, removeItem: key => { stored.delete(key); } };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const base = { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined };
    const imported = render(DaxLab, { ...base, importedMeasures: [{ TableName: "# Measures", Name: "Total", Expression: "1" }], importedTables: ["# Measures", "Sales"] });
    assert.ok(imported.includes("Queue as a measure") && imported.includes("Table for the new measure") && imported.includes("Number format of the new measure"));
    assert.ok(!imported.includes("Save to draft") && !imported.includes("Create a draft to save measures."));
    assert.match(imported, /aria-label="New measure name"[^>]*value="Margin"|value="Margin"[^>]*aria-label="New measure name"/);
    const plain = render(DaxLab, { ...base });
    assert.ok(plain.includes("Save to draft") && plain.includes("Create a draft to save measures.") && plain.includes("Saved measures require review before building."));
    assert.ok(!plain.includes("Queue as a measure") && !plain.includes("Table for the new measure"));
  } finally {
    delete globalThis.localStorage;
  }
});

test("DAX chat controls are discoverable with no history and the typed message survives remount", async () => {
  const stored = new Map([["studio.dax.composer.v1:w:s", "My unsent question"], ["studio.dax.w", "1 + 2"]]);
  globalThis.localStorage = { getItem: key => stored.get(key) ?? null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const base = { workspaceId: "w", snapshot: { id: "s", tables: [] }, busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, durableHistory: true };
    const html = render(DaxLab, base);
    assert.match(html, /aria-label="Chat history"/);
    assert.match(html, /disabled=""[^>]*title="Save the current chat[^>]*>.*?New chat/s);
    assert.ok(html.includes("My unsent question") && html.includes("1 + 2"));
    assert.doesNotMatch(html, /Confirm new DAX chat|Continue this chat/);
    assert.ok(render(DaxLab, base).includes("My unsent question"));
    assert.ok(!render(DaxLab, { ...base, workspaceId: "other" }).includes("My unsent question"));
  } finally { delete globalThis.localStorage; }
});

test("long DAX history keeps the newest complete exchanges visible and offers earlier messages", async () => {
  globalThis.localStorage = { getItem: () => null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const history = Array.from({ length: 31 }, (_, index) => ({ id: `request-${index}`, goal: `QUESTION-${index}-END`, response: `ANSWER-${index}-END`, kind: "advice" }));
    const html = render(DaxLab, { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, history, durableHistory: true });
    assert.ok(html.includes("Show earlier messages"));
    assert.ok(!html.includes("QUESTION-0-END") && !html.includes("ANSWER-0-END"));
    assert.ok(html.includes("QUESTION-7-END") && html.includes("ANSWER-7-END") && html.includes("QUESTION-30-END") && html.includes("ANSWER-30-END"));
    assert.equal((html.match(/aria-label="Copy DAX response"/g) || []).length, 24);
  } finally { delete globalThis.localStorage; }
});

test("an incomplete response keeps its evidence visible without offering an applicable formula", async () => {
  globalThis.localStorage = { getItem: () => null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const formula = { name: "Partial", expression: "1 + 2" };
    const history = [{ id: "stopped", goal: "Calculate a value", response: "```dax\nPartial = 1 + 2\n```", kind: "formula", status: "cancelled", formula, calculationRequest: "Generate this calculation" }];
    const html = render(DaxLab, { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, history, durableHistory: true });
    assert.ok(html.includes("Stopped response") && html.includes("Not completed") && html.includes("Partial = 1 + 2"));
    assert.doesNotMatch(html, />Use formula<|>Create measure<|>Draft formula</);
  } finally { delete globalThis.localStorage; }
});

test("a guarded clarification identifies the request outcome without suggesting the current formula is invalid", async () => {
  globalThis.localStorage = { getItem: key => key === "studio.dax.w" ? "SUM('Orders'[Sales])" : null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const history = [{ id: "clarify", goal: "Build a combined calculation", response: "This pattern does not represent all the selected calculation options. No formula or report change was applied.", kind: "advice", status: "completed" }];
    const html = render(DaxLab, { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, history, durableHistory: true });
    assert.ok(html.includes("Needs clarification") && html.includes("Current formula retained"));
    assert.match(html, /aria-label="DAX expression"[^>]*>SUM\(&#x27;Orders&#x27;\[Sales\]\)<\/textarea>/);
    assert.doesNotMatch(html, />Use formula<|>Create measure<|Formula ready to test/);
  } finally { delete globalThis.localStorage; }
});

test("a generated formula is identified as a testable candidate with a separate explicit save", async () => {
  globalThis.localStorage = { getItem: () => null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const history = [{ id: "formula", goal: "Calculate a total", response: "```dax\nTotal = 1 + 2\n```", kind: "formula", formula: { name: "Total", expression: "1 + 2" }, status: "completed" }];
    const html = render(DaxLab, { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, history });
    assert.ok(html.includes("Formula ready to test") && html.includes("Save or queue it separately"));
    assert.ok(html.includes("Use formula") && html.includes("Create measure"));
    assert.ok(!html.includes('Saved &quot;Total&quot; to the draft'));
  } finally { delete globalThis.localStorage; }
});

test("shared-panel mode leaves only the DAX workbench when its portal host is not yet mounted", async () => {
  globalThis.localStorage = { getItem: key => key === "studio.dax.w" ? "1 + 2" : null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const base = { workspaceId: "w", snapshot: { id: "s", tables: [] }, busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined };
    const shared = render(DaxLab, { ...base, externalConversation: true, conversationHost: null });
    assert.ok(shared.includes("external-conversation") && shared.includes('aria-label="Formula and preview"') && shared.includes('aria-label="DAX expression"'));
    assert.doesNotMatch(shared, /aria-label="DAX conversation"|aria-label="Message DAX AI"/);
    const embedded = render(DaxLab, base);
    assert.match(embedded, /aria-label="DAX conversation"/);
    assert.equal((embedded.match(/aria-label="Message DAX AI"/g) || []).length, 1);
  } finally { delete globalThis.localStorage; }
});

test("a handoff shows the original Workspace question and a return path beside the current chat", async () => {
  globalThis.localStorage = { getItem: () => null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const html = render(DaxLab, { workspaceId: "w", busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined,
      handoff: { request: "Sales for West", sourceLabel: "From Workspace chat", code: "SUM('Orders'[Sales])", sourceMessageId: 42 }, handoffStorageWarning: "The source request could not be saved. Keep this tab open.", onBackToSource: () => undefined, onClearHandoff: () => undefined });
    assert.ok(html.includes("From Workspace chat") && html.includes("Sales for West") && html.includes("Original request included in this DAX chat."));
    assert.ok(html.includes("Back to source chat") && html.includes('aria-label="Detach source request"'));
    assert.match(html, /<details class="dax-handoff-formula"><summary>Source formula<\/summary>/);
    assert.ok(html.includes("SUM(&#x27;Orders&#x27;[Sales])"));
    assert.match(html, /class="dax-handoff-storage" role="status">The source request could not be saved/);
    assert.match(html, /aria-label="DAX expression"[^>]*><\/textarea>/, "reading the source candidate does not apply it to the scratchpad");
  } finally { delete globalThis.localStorage; }
});

test("New chat is available for a handoff-only conversation while an ordinary empty chat stays disabled", async () => {
  globalThis.localStorage = { getItem: key => key === "studio.dax.w" ? "1 + 2" : key === "studio.dax.composer.v1:w:s" ? "My typed refinement" : null };
  try {
    const { DaxLab } = await load("DaxLab.tsx");
    const base = { workspaceId: "w", snapshot: { id: "s", tables: [] }, busy: false, onRun: async () => ({}), onSave: async () => undefined, onAsk: async () => ({}), onDismissSuggestion: () => undefined, durableHistory: true };
    const newChatButton = html => html.match(/<button[^>]*title="Save the current chat[^>]*>[\s\S]*?New chat<\/button>/)?.[0];
    assert.match(newChatButton(render(DaxLab, base)), /disabled=""/);
    const handoff = { request: "Calculate a total", sourceLabel: "Workspace chat", code: "1 + 2" };
    const html = render(DaxLab, { ...base, handoff, onClearHandoff: () => undefined });
    assert.ok(newChatButton(html), "the source can be detached without a completed DAX turn");
    assert.doesNotMatch(newChatButton(html), /disabled=""/);
    assert.ok(html.includes("My typed refinement") && html.includes("1 + 2"));
    assert.match(newChatButton(render(DaxLab, { ...base, handoff, busy: true })), /disabled=""/);
  } finally { delete globalThis.localStorage; }
});
