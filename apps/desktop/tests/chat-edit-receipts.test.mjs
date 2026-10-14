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
const editing = await load("importedEditing.ts");
const { editCardsFromEvents, mergeEditCards, queueableConfirm, parseChatEditEvent, addEditCard, receiptCanUndo, createPendingStore } = editing;
const { renderToStaticMarkup } = await import("react-dom/server");
const React = await import("react");

const applied = (revision, summary, extra = {}) => ({ event: "edit_applied", data: { revision_id: revision, parent_id: "r0", head: { id: revision, hash: "h" }, ops: [{ op: "set_position", visual: "v", x: 40 }], summaries: [summary], warnings: [], visible_in: [], protected: [], can_undo: true, ...extra } });
const turn = (sequence, edit_events, type = "chat.action") => ({ sequence, type, created_at: "", payload: { request_id: `q${sequence}`, text: "Done.", draft_id: null, edit_events } });

test("receipts of earlier chat turns are read back from the activity log", () => {
  const events = [
    turn(3, [{ event: "edit_progress", data: { stage: "writing", label: "Writing" } }, applied("r0001", "Made the bars green.")]),
    { sequence: 4, type: "dax.assistant_answered", created_at: "", payload: { edit_events: [applied("r9999", "not a chat turn")] } },
    turn(5, [{ event: "edit_choices", data: { question: "Which chart?", choices: [{ index: 0, label: "A" }], pending_id: "p" } }, { event: "edit_failed", data: { message: "No." } }]),
    turn(6, [applied("r0002", "Moved the card."), applied("r0003", "Renamed the page.")]),
    turn(7, "not a list"),
    turn(8, [{ nope: true }, null, { event: "edit_applied", data: { summaries: ["no revision id"] } }]),
  ];
  const cards = editCardsFromEvents(events);
  assert.deepEqual(cards.map(card => card.type), ["applied", "applied", "applied"]);
  assert.deepEqual(cards.map(card => card.event.revisionId), ["r0001", "r0002", "r0003"]);
  assert.deepEqual(cards.map(card => card.id), ["recorded-3-1", "recorded-6-0", "recorded-6-1"]);
  assert.deepEqual(cards[0].event.summaries, ["Made the bars green."]);
  // Undo is offered only on the receipt that made the current head.
  assert.deepEqual(cards.map(card => receiptCanUndo(card, "r0003")), [false, false, true]);
  assert.deepEqual(editCardsFromEvents([]), []);
});

test("only the newest forty receipts are kept", () => {
  const events = Array.from({ length: 50 }, (_, i) => turn(i + 1, [applied(`r${i + 1}`, `Change ${i + 1}.`)]));
  const cards = editCardsFromEvents(events);
  assert.equal(cards.length, 40);
  assert.equal(cards[0].event.revisionId, "r11");
  assert.equal(cards[39].event.revisionId, "r50");
});

test("a receipt in the log and in this session shows once, as the session's card", () => {
  const recorded = editCardsFromEvents([turn(3, [applied("r0001", "First.")]), turn(4, [applied("r0002", "Second.")])]);
  const session = addEditCard([], parseChatEditEvent("edit_applied", applied("r0002", "Second.").data), "edit-card-1").map(card => ({ ...card, undone: true }));
  const merged = mergeEditCards(recorded, session);
  assert.deepEqual(merged.map(card => card.id), ["recorded-3-0", "edit-card-1"]);
  assert.equal(merged[1].undone, true);
  assert.deepEqual(mergeEditCards([], []), []);
});

test("a measure change waiting for confirmation can be queued with its what-if answer", () => {
  const card = (kind, ops, extra = {}) => ({ id: "c", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind, ops, detail: "d", pending_id: "p", what_if: { question: "target 120K", answer: "41 agents" }, ...extra }), ...extra.card });
  const measureOps = [{ op: "update_measure", name: "Target", expression: "120000" }, { op: "create_measure", table: "T", name: "New", expression: "1" }];
  const found = queueableConfirm(card("model_edit", measureOps));
  assert.deepEqual(found.ops.map(op => op.op), ["update_measure", "create_measure"]);
  assert.deepEqual(found.whatIf, { question: "target 120K", answer: "41 agents" });
  // Not a measure change, mixed with a report change, empty, another kind of question, already answered, or not a confirmation.
  assert.equal(queueableConfirm(card("model_edit", [{ op: "set_position", visual: "v" }])), undefined);
  assert.equal(queueableConfirm(card("model_edit", [...measureOps, { op: "set_position", visual: "v" }])), undefined);
  assert.equal(queueableConfirm(card("model_edit", [])), undefined);
  assert.equal(queueableConfirm(card("restore", measureOps)), undefined);
  assert.equal(queueableConfirm({ ...card("model_edit", measureOps), answered: "applied" }), undefined);
  assert.equal(queueableConfirm({ id: "x", type: "failed", event: { type: "failed", message: "m" } }), undefined);
  // What the tray then holds: the first change carries the what-if answer.
  const store = createPendingStore(() => "id");
  found.ops.forEach((op, index) => store.add("w", op, index === 0 && found.whatIf ? { whatIf: found.whatIf } : {}));
  const held = store.get("w");
  assert.equal(held.length, 2);
  assert.deepEqual(held[0].whatIf, { question: "target 120K", answer: "41 agents" });
  assert.equal(held[1].whatIf, undefined);
});

test("the conversation shows recorded receipts and offers to queue a measure change", async () => {
  const { useChatEdits } = await load("useChatEdits.ts");
  const { EditChatCards } = await load("EditChatCards.tsx");
  const events = [turn(3, [applied("r0001", "Made the bars green.")]), turn(4, [applied("r0002", "Moved the card.")])];
  function Probe({ list }) {
    const edits = useChatEdits({ workspaceId: "w", headId: "r0002", refresh: () => undefined, undo: () => undefined, events: list });
    return React.createElement(EditChatCards, { ...edits.panel, cards: [...edits.panel.cards, { id: "q", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind: "model_edit", ops: [{ op: "delete_measure", name: "Old" }], detail: "d", pending_id: "p" }) }] });
  }
  const html = renderToStaticMarkup(React.createElement(Probe, { list: events }));
  assert.ok(html.includes("Made the bars green.") && html.includes("Moved the card."));
  // Only the receipt of the current head has a live Undo.
  const undo = [...html.matchAll(/<button[^>]*>(?:<svg[^>]*>.*?<\/svg>)?Undo<\/button>/g)].map(m => /disabled=""/.test(m[0]));
  assert.deepEqual(undo, [true, false]);
  assert.ok(html.includes("Add to pending changes"));
  assert.ok(!renderToStaticMarkup(React.createElement(Probe, { list: undefined })).includes("Made the bars green."));
});

test("New chat in the log hides the receipts before it after a reload", async () => {
  const { useChatEdits } = await load("useChatEdits.ts");
  const cleared = (sequence) => ({ sequence, type: "chat.cleared", created_at: "", payload: { through_message_id: 2 } });
  const shown = (list) => {
    let cards = [];
    function Probe() { cards = useChatEdits({ workspaceId: "w", headId: "r0002", refresh: () => undefined, undo: () => undefined, events: list }).panel.cards; return null; }
    renderToStaticMarkup(React.createElement(Probe));
    return cards.map(card => card.event.summaries[0]);
  };
  assert.deepEqual(shown([turn(3, [applied("r0001", "Made the bars green.")]), cleared(4)]), []);
  assert.deepEqual(shown([turn(3, [applied("r0001", "Made the bars green.")]), cleared(4), turn(5, [applied("r0002", "Moved the card.")])]), ["Moved the card."]);
  assert.equal(editing.lastChatClear([turn(1, []), cleared(4), cleared(9), turn(10, [])]), 9);
  assert.equal(editing.lastChatClear([]), 0);
});

test("the activity log is read in pages until a short page comes back", async () => {
  const log = Array.from({ length: 600 }, (_, i) => ({ sequence: i + 1, type: "x", payload: {} }));
  const asked = [];
  const all = await editing.loadAllEvents(async (after) => { asked.push(after); return log.filter(event => event.sequence > after).slice(0, 250); });
  assert.equal(all.length, 600);
  assert.equal(all[599].sequence, 600);
  assert.deepEqual(asked, [0, 250, 500]);
  // Exactly one full page needs one more request to learn there is nothing else.
  const two = [];
  assert.equal((await editing.loadAllEvents(async (after) => { two.push(after); return log.filter(event => event.sequence > after && event.sequence <= 250).slice(0, 250); })).length, 250);
  assert.deepEqual(two, [0, 250]);
  assert.deepEqual(await editing.loadAllEvents(async () => []), []);
  // A server that keeps repeating a page cannot loop forever.
  let calls = 0;
  await editing.loadAllEvents(async () => { calls += 1; return log.slice(0, 250); });
  assert.equal(calls, 2);
});

test("queueing the same new measure name twice keeps one entry, the newest", () => {
  const store = createPendingStore(() => `id${Math.random()}`);
  store.add("w", { op: "create_measure", table: "T", name: "Margin", expression: "1" });
  store.add("w", { op: "create_measure", table: "T", name: "margin", expression: "2" });
  store.add("w", { op: "create_measure", table: "T", name: "Other", expression: "3" });
  const held = store.get("w");
  assert.deepEqual(held.map(item => [item.op.name, item.op.expression]), [["margin", "2"], ["Other", "3"]]);
});
