import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

async function load(file) {
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../src/${file}`, import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
}
const editing = await load("importedEditing.ts");
const geometry = await load("importedGeometry.ts");
const format = await load("importedFormat.ts");
const {
  createEditingClient, classifyEditProblem, problemUi, readReceipt, describeReceipt, historyView, redoTarget, restorePrompt, readHistory,
  themeSwatches, themeColorValue, shadeHex, normalizeHex, pushRecentColor, deliveryStatus, defaultCopyName, createPendingStore,
  parseChatEditEvent, addEditCard, receiptCanUndo, stageLabel, describePending, readChatAnswer,
} = editing;

// --- a scripted transport --------------------------------------------------------------------------------------

function script(...answers) {
  const calls = [];
  const transport = async (method, path, body) => {
    calls.push({ method, path, body });
    const next = answers.shift();
    if (!next) throw new Error(`unexpected call ${method} ${path}`);
    if (next instanceof Error) throw next;
    return typeof next === "function" ? next(body) : next;
  };
  return { transport, calls };
}
const receiptBody = (id = "r0008", extra = {}) => ({
  revision: { id, parent: "r0007", author: "studio", summary: "Moved the chart.", created: "2026-09-28T10:00:00Z" },
  head: { id, hash: `hash-${id}` }, summaries: ["Moved the 'MTD Sales by Group' chart."], warnings: [], adopted: null, protected: [],
  visible_in: [{ op: "set_position", where: "studio" }], outcome: "updated", desktop: "closed", ...extra,
});
let counter = 0;
const options = (extra = {}) => ({ key: () => `key-${String(++counter).padStart(8, "0")}`, sleep: async () => undefined, ...extra });

// --- requests: key, head, polling ----------------------------------------------------------------------------------

test("an edit sends the operations with the expected head and one idempotency key", async () => {
  const { transport, calls } = script({ status: 200, body: receiptBody() });
  const client = createEditingClient(transport, options());
  const attempt = client.newAttempt([{ op: "set_number_format", visual: "b404", preset: "percent" }], "r0007", { summary: "Percent labels" });
  const result = await client.edit("ws 1", attempt);
  assert.equal(result.ok, true);
  assert.equal(result.receipt.head.id, "r0008");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "POST");
  assert.equal(calls[0].path, "/v1/workspaces/ws%201/imported-report/edits");
  assert.deepEqual(calls[0].body, { operations: [{ op: "set_number_format", visual: "b404", preset: "percent" }], expected_head: "r0007", summary: "Percent labels", idempotency_key: attempt.key });
  assert.match(attempt.key, /^key-\d{8}$/);
});

test("a 202 is polled until done, reporting progress, and never re-posts the batch", async () => {
  const seen = [];
  const { transport, calls } = script(
    { status: 202, body: { batch_id: "b1", state: "queued" } },
    { status: 200, body: { state: "running", stage: "closing_desktop" } },
    { status: 200, body: { state: "running", stage: "writing", label: "Writing the change" } },
    { status: 200, body: { state: "done", receipt: receiptBody("r0009") } },
  );
  const client = createEditingClient(transport, options({ onProgress: p => seen.push(p) }));
  const result = await client.edit("w", client.newAttempt([{ op: "set_alt_text", visual: "v", text: "Hi" }], "r0008"));
  assert.equal(result.ok, true);
  assert.equal(result.receipt.head.id, "r0009");
  assert.deepEqual(calls.map(c => `${c.method} ${c.path}`), [
    "POST /v1/workspaces/w/imported-report/edits", "GET /v1/workspaces/w/imported-report/edits/b1",
    "GET /v1/workspaces/w/imported-report/edits/b1", "GET /v1/workspaces/w/imported-report/edits/b1",
  ]);
  assert.deepEqual(seen.map(p => p.stage), [undefined, "closing_desktop", "writing", undefined]);
});

test("a failed batch becomes a problem, and a slow one offers to keep waiting", async () => {
  let clock = 0;
  const failed = script({ status: 202, body: { batch_id: "b2" } }, { status: 200, body: { state: "failed", error: { code: "edit_rejected", message: "Agent ID has no totals.", step: "set_table_layout" } } });
  const a = createEditingClient(failed.transport, options());
  const failure = await a.edit("w", a.newAttempt([{ op: "set_table_layout", visual: "v", totals: true }], "r1"));
  assert.deepEqual(failure, { ok: false, problem: { kind: "rejected", message: "Agent ID has no totals.", step: "set_table_layout" } });

  const slow = script({ status: 202, body: { batch_id: "b3" } }, { status: 200, body: { state: "running" } }, { status: 200, body: { state: "running" } }, { status: 200, body: { state: "done", receipt: receiptBody("r0010") } });
  const b = createEditingClient(slow.transport, options({ pollTimeoutMs: 50, now: () => clock, sleep: async () => { clock += 60; } }));
  const waiting = await b.edit("w", b.newAttempt([{ op: "arrange", visual: "v", to: "front" }], "r1"));
  assert.equal(waiting.ok, false);
  assert.equal(waiting.problem.kind, "still_running");
  assert.equal(waiting.problem.batchId, "b3");
  clock = 0;
  const done = await b.wait("w", "b3");
  assert.equal(done.ok, true);
  assert.equal(done.receipt.head.id, "r0010");
});

test("errors map to the UI state the brief asks for", () => {
  assert.deepEqual(classifyEditProblem(409, { detail: { code: "head_moved", message: "x", expected: "r1", actual: "r2" } }), { kind: "head_moved", message: "x", expected: "r1", actual: "r2" });
  assert.equal(classifyEditProblem(409, { code: "desktop_unsaved", choices: ["save_and_continue", "cancel"] }).kind, "desktop_unsaved");
  assert.deepEqual(classifyEditProblem(409, { code: "desktop_dialog", dialog: "Save changes?" }), { kind: "desktop_dialog", message: "Power BI Desktop is showing a dialog.", dialog: "Save changes?" });
  assert.equal(classifyEditProblem(409, { code: "edit_busy" }).kind, "busy");
  const confirm = classifyEditProblem(409, { code: "needs_confirmation", kind: "replace_conditional", detail: "This replaces conditional formatting on Agent ID." });
  assert.equal(confirm.kind, "needs_confirmation");
  assert.equal(confirm.confirm, "replace_conditional");
  assert.equal(problemUi(confirm).body, "This replaces conditional formatting on Agent ID.");
  assert.deepEqual(problemUi(confirm).actions, ["keep", "replace"]);
  assert.deepEqual(classifyEditProblem(422, { code: "edit_rejected", message: "A card has no totals.", step: "set_table_layout" }), { kind: "rejected", message: "A card has no totals.", step: "set_table_layout" });
  assert.equal(problemUi(classifyEditProblem(422, { detail: "Plain FastAPI message" })).body, "Plain FastAPI message");
  const unsupported = classifyEditProblem(422, { code: "edit_unsupported", capability: "bookmarks", reason: "Bookmarks are not editable yet.", desktop_steps: ["Open the View tab", "Choose Bookmarks"] });
  assert.equal(unsupported.kind, "unsupported");
  assert.deepEqual(problemUi(unsupported).steps, ["Open the View tab", "Choose Bookmarks"]);
  assert.equal(classifyEditProblem(503, { code: "desktop_unavailable", message: "Power BI Desktop is not installed." }).kind, "desktop_unavailable");
  assert.equal(classifyEditProblem(500, "boom").message, "boom");
  // Cancel is the default (first) action of the unsaved-work dialog: Studio never saves the user's work by accident.
  assert.deepEqual(problemUi({ kind: "desktop_unsaved", message: "" }).actions, ["cancel", "save_and_continue"]);
  assert.match(problemUi({ kind: "desktop_unsaved", message: "" }).title, /unsaved changes to this report/);
});

test("retries: unsaved work reuses the key, a moved head or a confirmation gets a fresh one", async () => {
  const { transport, calls } = script(
    { status: 409, body: { code: "desktop_unsaved" } },
    { status: 200, body: receiptBody("r0008") },
    { status: 409, body: { code: "head_moved", expected: "r0007", actual: "r0008" } },
    { status: 200, body: receiptBody("r0009") },
  );
  const client = createEditingClient(transport, options());
  const attempt = client.newAttempt([{ op: "set_alt_text", visual: "v", text: "A" }], "r0007");
  const first = await client.edit("w", attempt);
  assert.equal(first.problem.kind, "desktop_unsaved");
  const saved = client.withOptions(attempt, { desktop_work: "save" }, true);
  assert.equal(saved.key, attempt.key);
  assert.equal((await client.edit("w", saved)).ok, true);
  assert.equal(calls[1].body.idempotency_key, calls[0].body.idempotency_key);
  assert.equal(calls[1].body.desktop_work, "save");
  assert.equal(calls[0].body.desktop_work, undefined);

  const moved = client.newAttempt([{ op: "set_alt_text", visual: "v", text: "B" }], "r0007");
  assert.equal((await client.edit("w", moved)).problem.kind, "head_moved");
  const redo = client.rebase(moved, "r0008");
  assert.notEqual(redo.key, moved.key);
  assert.deepEqual(redo.request.operations, moved.request.operations);
  assert.equal(redo.request.expected_head, "r0008");
  await client.edit("w", redo);
  assert.equal(calls[3].body.expected_head, "r0008");
  const replace = client.withOptions(redo, { confirm_replace: true }, false);
  assert.notEqual(replace.key, redo.key);
  assert.equal(replace.request.confirm_replace, true);
});

test("a dropped connection keeps the attempt, so a retry replays the same key", async () => {
  const { transport, calls } = script(new Error("Local engine request failed"), { status: 200, body: receiptBody("r0008") });
  const client = createEditingClient(transport, options());
  const attempt = client.newAttempt([{ op: "delete_visual", visual: "v" }], "r0007");
  const lost = await client.edit("w", attempt);
  assert.equal(lost.ok, false);
  assert.equal(lost.problem.kind, "network");
  assert.match(problemUi(lost.problem).body, /cannot be applied twice/);
  assert.deepEqual(problemUi(lost.problem).actions, ["retry", "dismiss"]);
  assert.equal((await client.edit("w", attempt)).ok, true);
  assert.equal(calls[0].body.idempotency_key, calls[1].body.idempotency_key);
});

test("undo, restore and history use the documented routes and bodies", async () => {
  const row = (id, extra = {}) => ({ id, parent: null, author: "studio", summary: id, created: "t", is_head: false, can_undo: false, operations: [], ...extra });
  const { transport, calls } = script(
    { status: 200, body: receiptBody("r0009") },
    { status: 200, body: receiptBody("r0010") },
    { status: 200, body: [row("r0002", { is_head: true, can_undo: true })] },
    { status: 200, body: { would_discard: [row("r0003"), row("r0004", { author: "desktop" })] } },
  );
  const client = createEditingClient(transport, options());
  await client.undo("w", { key: "undo-key-1", expected_head: "r0008" });
  await client.restore("w", { key: "restore-key-1", revision_id: "r0004", expected_head: "r0009", confirmed_discard: true, desktop_work: "save" });
  const history = await client.history("w");
  const later = await client.discardPreview("w", "r0002");
  assert.deepEqual(calls[0], { method: "POST", path: "/v1/workspaces/w/imported-report/undo", body: { expected_head: "r0008", idempotency_key: "undo-key-1" } });
  assert.deepEqual(calls[1].body, { revision_id: "r0004", expected_head: "r0009", confirmed_discard: true, desktop_work: "save", idempotency_key: "restore-key-1" });
  assert.equal(calls[1].path, "/v1/workspaces/w/imported-report/restore");
  assert.equal(history[0].can_undo, true);
  assert.equal(calls[3].path, "/v1/workspaces/w/imported-report/history/r0002/discard-preview");
  assert.deepEqual(later.map(r => r.author), ["studio", "desktop"]);
});

test("delivery calls and the chat answers", async () => {
  const { transport, calls } = script(
    { status: 200, body: { path: "C:/x/Report (edited).pbix", hash: "h" } },
    { status: 409, body: { detail: { code: "edit_rejected", message: "The original file changed. Save a copy instead." } } },
    { status: 200, body: { receipt: receiptBody("r0011") } },
  );
  const client = createEditingClient(transport, options());
  assert.deepEqual(await client.saveCopy("w", "C:/x/Report (edited).pbix", "copy-key-01"), { ok: true, path: "C:/x/Report (edited).pbix", hash: "h", backup: undefined });
  assert.deepEqual(calls[0].body, { destination: "C:/x/Report (edited).pbix", idempotency_key: "copy-key-01" });
  const refused = await client.replaceOriginal("w", "replace-key");
  assert.deepEqual(refused, { ok: false, message: "The original file changed. Save a copy instead." });
  assert.deepEqual(calls[1].body, { idempotency_key: "replace-key", confirm: true });
  const answer = readChatAnswer(await client.answerConfirm("w", "p1", true));
  assert.equal(answer.receipt.head.id, "r0011");
  assert.equal(calls[2].path, "/v1/workspaces/w/chat/edit-confirm");
  assert.deepEqual(calls[2].body, { pending_id: "p1", accept: true });
});

// --- receipts ------------------------------------------------------------------------------------------------------------

test("a receipt says what changed and what only Power BI shows", () => {
  const receipt = readReceipt(receiptBody("r0008", {
    summaries: ["Showed the numbers in thousands.", "Turned the totals off."], warnings: ["Two visuals still use their own number format."],
    protected: [{ visual: "e82e", what: "conditional font color on Agent ID" }],
    visible_in: [{ op: "set_position", where: "studio" }, { op: "set_number_format", where: "powerbi_only" }, { op: "set_number_format", where: "powerbi_only" }, { op: "set_table_layout", where: "covered_by_background" }],
    desktop: "reopened",
  }));
  const view = describeReceipt(receipt);
  assert.equal(view.headline, "Made 2 changes");
  assert.equal(view.tone, "warn");
  assert.deepEqual(view.lines, ["Showed the numbers in thousands.", "Turned the totals off."]);
  assert.deepEqual(view.chips.map(c => c.label), ["Number format: visible in Power BI only", "Table layout: hidden behind the background", "Kept conditional font color on Agent ID"]);
  assert.equal(view.desktopNote, "Power BI was closed for this change and reopened with it.");
  assert.equal(describeReceipt(readReceipt(receiptBody())).headline, "Moved the 'MTD Sales by Group' chart.");
  assert.equal(describeReceipt(readReceipt(receiptBody("r9", { outcome: "partial" }))).headline, "Saved, with a problem afterwards");
  assert.equal(readReceipt({ revision: {}, head: {} }), undefined);
});

// --- history -----------------------------------------------------------------------------------------------------------

const H = (id, extra = {}) => ({ id, parent: null, author: "studio", summary: `change ${id}`, created: `t${id}`, is_head: false, can_undo: false, operations: [], ...extra });

test("Undo is offered only on the newest change; older rows offer a restore", () => {
  const rows = readHistory([H("r4", { is_head: true, can_undo: true }), H("r3"), H("r2", { author: "desktop", available: false }), H("r1", { author: "import" })]);
  const view = historyView(rows, "r4");
  assert.deepEqual(view.map(r => [r.id, r.canUndo, r.canRestore, r.keptOnDisk]), [["r4", true, false, true], ["r3", false, true, true], ["r2", false, false, false], ["r1", false, true, true]]);
  assert.deepEqual(view.map(r => r.badge.label), ["Studio", "Studio", "Power BI Desktop", "Import"]);
  // A stale head id (another edit landed) turns Undo off everywhere, even if the row still says can_undo.
  assert.equal(historyView(rows, "r5").some(r => r.canUndo), false);
});

test("restoring lists the later changes with their authors and needs a confirmation", () => {
  const later = readHistory([H("r4", { author: "desktop", summary: "Saved in Power BI" }), H("r3", { author: "undo", summary: "Undid a change" })]);
  const prompt = restorePrompt({ summary: "Made the bars green." }, later);
  assert.equal(prompt.needsConfirmation, true);
  assert.equal(prompt.title, "Go back to before “Made the bars green.”?");
  assert.match(prompt.body, /leaves 2 later changes out/);
  assert.deepEqual(prompt.items.map(i => i.author), ["Power BI Desktop", "Undo"]);
  assert.equal(prompt.confirmLabel, "Restore and leave out 2 changes");
  assert.equal(restorePrompt({ summary: "x" }, []).needsConfirmation, false);
});

test("redo is a restore of what the newest Undo took back", () => {
  const rows = readHistory([H("r5", { author: "undo", parent: "r4", is_head: true, restores: "r3" }), H("r4", { parent: "r3" }), H("r3", { parent: "r2" })]);
  assert.equal(redoTarget(rows, "r5"), "r4");
  assert.equal(redoTarget(rows, "r4"), undefined);
  assert.equal(redoTarget(readHistory([H("r5", { author: "undo", parent: "r4", is_head: true }), H("r4", { available: false })]), "r5"), undefined);
});

// --- colors ---------------------------------------------------------------------------------------------------------------

test("the color picker offers white, black and the first eight theme colors: ids 0 to 9 only", () => {
  const swatches = themeSwatches({ dataColors: ["#0b6e4f", "#f26419", "#33658a"], palette: ["#ffffff", "#000000", "#0b6e4f", "#f26419", "#33658a", "#111111", "#222222", "#333333", "#444444", "#555555", "#666666", "#777777"] });
  assert.equal(swatches.length, 10);
  assert.deepEqual(swatches.map(s => s.hex), ["#FFFFFF", "#000000", "#0B6E4F", "#F26419", "#33658A", "#111111", "#222222", "#333333", "#444444", "#555555"]);
  assert.deepEqual(swatches.slice(0, 3).map(s => s.label), ["White", "Black", "Theme color 1"]);
  assert.deepEqual(themeColorValue(9, -0.25), { theme: 9, percent: -0.25 });
  assert.throws(() => themeColorValue(10), RangeError);
  assert.throws(() => themeColorValue(-1), RangeError);
  // Without a theme payload the swatches still cover ids 0..9 with Power BI's default palette.
  assert.equal(themeSwatches(undefined).length, 10);
  assert.deepEqual(themeSwatches({ dataColors: ["#123456"] }).slice(2, 4).map(s => s.hex), ["#123456", "#123456"]);
  assert.deepEqual(themeSwatches(undefined).slice(2).map(s => s.hex), ["#118DFF", "#12239E", "#E66C37", "#6B007B", "#E044A7", "#744EC2", "#D9B300", "#D64550"]);
});

test("theme shading matches Power BI's half-up rounding and hex input is normalized", () => {
  assert.equal(shadeHex("#808080", -0.4), "#4D4D4D");
  assert.equal(shadeHex("#808080", 0.4), "#B3B3B3");
  assert.equal(shadeHex("#FFFFFF", -0.5), "#808080");
  assert.equal(shadeHex("#000000", 0.6), "#999999");
  assert.equal(normalizeHex("#0b6"), "#00BB66");
  assert.equal(normalizeHex("0B6E4F"), "#0B6E4F");
  assert.equal(normalizeHex("green"), undefined);
  assert.deepEqual(pushRecentColor(["#111111", "#222222"], "#222222"), ["#222222", "#111111"]);
  assert.deepEqual(pushRecentColor(["#111111"], "nope"), ["#111111"]);
  assert.equal(pushRecentColor(Array.from({ length: 8 }, (_, i) => `#00000${i}`), "#ABCDEF").length, 8);
});

// --- delivery ------------------------------------------------------------------------------------------------------------

test("the delivery line and the default copy name", () => {
  assert.deepEqual(deliveryStatus({ state: "edited", changes_not_in_source: 3 }), { text: "Edited in Studio · 3 changes are not in your file", tone: "warn", canSaveToFile: true, canSaveCopy: true });
  assert.equal(deliveryStatus({ state: "edited", changes_not_in_source: 1 }).text, "Edited in Studio · 1 change is not in your file");
  assert.equal(deliveryStatus({ state: "source_changed", changes_not_in_source: 2 }).canSaveToFile, false);
  assert.equal(deliveryStatus({ state: "in_sync", changes_not_in_source: 0 }).canSaveToFile, false);
  assert.equal(deliveryStatus(undefined).tone, "ok");
  assert.equal(defaultCopyName("C:\\Users\\a\\Downloads\\Agents Performance - Dashboard.pbix", "x"), "C:/Users/a/Downloads/Agents Performance - Dashboard (edited).pbix");
  assert.equal(defaultCopyName(undefined, "Sales.pbix"), "Sales (edited).pbix");
});

test("progress labels come from the server or the stage table", () => {
  assert.equal(stageLabel("closing_desktop"), "Closing Power BI");
  assert.equal(stageLabel("opening_desktop"), "Opening Power BI");
  assert.equal(stageLabel("writing", "Writing your change"), "Writing your change");
  assert.equal(stageLabel(undefined), "Working");
});

// --- pending model changes --------------------------------------------------------------------------------------------------

test("model changes queue, fold into each other and never leave the tray until applied", () => {
  let id = 0;
  const store = createPendingStore(() => `p${++id}`);
  let notified = 0;
  store.subscribe(() => { notified += 1; });
  const empty = store.get("w");
  assert.equal(store.get("other"), empty);
  store.add("w", { op: "create_measure", table: "Sales", name: "Margin", expression: "1", format_string: "0.0%" });
  store.add("w", { op: "update_measure", name: "Margin", expression: "DIVIDE([Profit],[Sales])" });
  store.add("w", { op: "update_measure", name: "Existing", expression: "2" });
  assert.deepEqual(store.get("w").map(c => c.title), ["Add the measure Margin to Sales", "Change the formula of Existing"]);
  assert.deepEqual(store.get("w")[0].op, { op: "create_measure", table: "Sales", name: "Margin", expression: "DIVIDE([Profit],[Sales])", format_string: "0.0%" });
  assert.equal(store.get("w")[0].dax, "DIVIDE([Profit],[Sales])");
  store.add("w", { op: "delete_measure", name: "Margin" });
  assert.deepEqual(store.get("w").map(c => c.op.name), ["Existing"]);
  store.add("w", { op: "rename_measure", name: "Existing", new_name: "Renamed" }, { whatIf: { question: "q", answer: "a" } });
  assert.equal(describePending({ op: "rename_measure", name: "Existing", new_name: "Renamed" }), "Rename Existing to Renamed");
  assert.deepEqual(store.get("w")[1].whatIf, { question: "q", answer: "a" });
  store.remove("w", store.get("w")[0].id);
  assert.equal(store.get("w").length, 1);
  store.clear("w");
  assert.equal(store.get("w").length, 0);
  assert.ok(notified >= 6);
});

// --- chat events ------------------------------------------------------------------------------------------------------------

test("chat events are parsed strictly and become cards; Undo only on the current head", () => {
  assert.deepEqual(parseChatEditEvent("edit_progress", { stage: "opening_desktop" }), { type: "progress", stage: "opening_desktop", label: "Opening Power BI" });
  const applied = parseChatEditEvent("edit_applied", { revision_id: "r0008", parent_id: "r0007", head: { id: "r0008", hash: "h" }, ops: [{ op: "set_conditional_format", visual: "v" }, "junk"], summaries: ["Colored it by rule."], warnings: [], visible_in: [{ op: "set_conditional_format", where: "powerbi_only" }], can_undo: true });
  assert.equal(applied.head.id, "r0008");
  assert.deepEqual(applied.ops, [{ op: "set_conditional_format", visual: "v" }]);
  assert.equal(applied.visibleIn[0].where, "powerbi_only");
  assert.equal(parseChatEditEvent("edit_applied", { summaries: [] }), undefined);
  const choices = parseChatEditEvent("edit_choices", { question: "Which chart?", choices: [{ index: 0, label: "Sales by Region" }, { index: 1, label: "Sales by Month" }, { nope: 1 }], pending_id: "p1" });
  assert.deepEqual(choices.choices.map(c => c.index), [0, 1]);
  assert.equal(parseChatEditEvent("edit_choices", { question: "x", choices: [], pending_id: "p" }), undefined);
  const confirm = parseChatEditEvent("edit_confirm", { kind: "model_edit", what_if: { question: "if the target were 120K", answer: "41 agents" }, ops: [{ op: "update_measure", name: "T" }], detail: "Target = 120000", pending_id: "p2" });
  assert.deepEqual(confirm.whatIf, { question: "if the target were 120K", answer: "41 agents" });
  assert.equal(parseChatEditEvent("edit_confirm", { kind: "model_edit" }), undefined);
  assert.deepEqual(parseChatEditEvent("edit_failed", { message: "No such visual", code: "edit_rejected" }), { type: "failed", message: "No such visual", code: "edit_rejected" });
  assert.equal(parseChatEditEvent("token", { text: "x" }), undefined);

  let cards = addEditCard([], parseChatEditEvent("edit_progress", { stage: "writing" }), "c0");
  assert.equal(cards.length, 0);
  cards = addEditCard(cards, applied, "c1");
  cards = addEditCard(cards, parseChatEditEvent("edit_applied", { revision_id: "r0009", head: { id: "r0009", hash: "" }, summaries: ["Later."] }), "c2");
  assert.deepEqual(cards.map(c => receiptCanUndo(c, "r0009")), [false, true]);
  assert.equal(receiptCanUndo({ ...cards[1], undone: true }, "r0009"), false);
  assert.equal(receiptCanUndo(addEditCard([], choices, "x")[0], "r0009"), false);
  assert.equal(receiptCanUndo(cards[1], undefined), false);
});

// --- geometry -------------------------------------------------------------------------------------------------------------------

const PAGE = { width: 1280, height: 720 };
const R = (x, y, width, height) => ({ x, y, width, height });

test("moving snaps to a 5 px grid unless Shift is held, and stays on the page", () => {
  const start = { a: R(100, 100, 200, 100) };
  assert.deepEqual(geometry.moveRects(start, ["a"], 12, 8, PAGE, { snap: true }), { a: R(110, 110, 200, 100) });
  assert.deepEqual(geometry.moveRects(start, ["a"], 12.4, 7.6, PAGE, { snap: false }), { a: R(112.4, 107.6, 200, 100) });
  assert.deepEqual(geometry.moveRects(start, ["a"], -500, -500, PAGE, { snap: true }), { a: R(0, 0, 200, 100) });
  assert.deepEqual(geometry.moveRects(start, ["a"], 5000, 5000, PAGE, { snap: true }), { a: R(1080, 620, 200, 100) });
});

test("a multi-selection moves together, keeping offsets and hitting the page edge as one", () => {
  const start = { a: R(0, 0, 100, 50), b: R(300, 100, 100, 50) };
  assert.deepEqual(geometry.moveRects(start, ["a", "b"], -40, 20, PAGE, { snap: true }), { a: R(0, 20, 100, 50), b: R(300, 120, 100, 50) });
  assert.deepEqual(geometry.moveRects(start, ["a", "b", "ghost"], 10, 0, PAGE, { snap: true }), { a: R(10, 0, 100, 50), b: R(310, 100, 100, 50) });
});

test("resize handles change the right edges, snap, respect a minimum and the page", () => {
  const start = R(100, 100, 200, 100);
  assert.deepEqual(geometry.resizeRect(start, "se", 33, 12, PAGE, { snap: true }), R(100, 100, 235, 110));
  assert.deepEqual(geometry.resizeRect(start, "nw", 20, 10, PAGE, { snap: true }), R(120, 110, 180, 90));
  assert.deepEqual(geometry.resizeRect(start, "e", -500, 99, PAGE, { snap: true }), R(100, 100, 10, 100));
  assert.deepEqual(geometry.resizeRect(start, "w", 250, 0, PAGE, { snap: true }), R(290, 100, 10, 100));
  assert.deepEqual(geometry.resizeRect(start, "s", 0, 9999, PAGE, { snap: true }), R(100, 100, 200, 620));
  assert.deepEqual(geometry.resizeRect(start, "n", 0, 7.3, PAGE, { snap: false }), R(100, 107.3, 200, 92.7));
  assert.deepEqual(geometry.resizeRect(start, "ne", 5, -5, PAGE, { snap: true }), R(100, 95, 205, 105));
});

test("arrow keys nudge 1 px, Shift 10 px, without snapping", () => {
  const start = { a: R(101, 103, 50, 50) };
  assert.deepEqual(geometry.nudgeRects(start, ["a"], "ArrowRight", false, PAGE), { a: R(102, 103, 50, 50) });
  assert.deepEqual(geometry.nudgeRects(start, ["a"], "ArrowUp", true, PAGE), { a: R(101, 93, 50, 50) });
  assert.deepEqual(geometry.nudgeRects(start, ["a"], "ArrowLeft", true, PAGE), { a: R(91, 103, 50, 50) });
  assert.deepEqual(geometry.nudgeRects(start, ["a"], "x", false, PAGE), {});
  assert.equal(geometry.isNudgeKey("ArrowDown"), true);
  assert.equal(geometry.isNudgeKey("Enter"), false);
});

test("set_position operations carry absolute page pixels for what changed, and a move never resizes", () => {
  const base = { a: R(100, 100, 200, 100), b: R(0, 0, 50, 50), c: R(10, 10, 20, 20) };
  const ops = geometry.positionOps(base, { a: R(110, 100, 200, 100), b: R(0, 0, 60, 50), c: R(10, 10, 20, 20), d: R(5, 5, 30, 30) });
  assert.deepEqual(ops, [
    { op: "set_position", visual: "a", x: 110, y: 100 },
    { op: "set_position", visual: "b", x: 0, y: 0, width: 60, height: 50 },
    { op: "set_position", visual: "d", x: 5, y: 5, width: 30, height: 30 },
  ]);
});

test("twelve nudges become one batch, and a mouse gesture flushes at once", () => {
  const timers = []; const flushed = [];
  const scheduler = { set: (fn, ms) => { const t = { fn, ms, live: true }; timers.push(t); return t; }, clear: t => { t.live = false; } };
  const coalescer = new geometry.GestureCoalescer(changes => flushed.push(changes), 2000, scheduler);
  let rect = R(100, 100, 50, 50);
  for (let i = 0; i < 12; i++) { rect = { ...rect, x: rect.x + 1 }; coalescer.stage({ a: rect }, "idle"); }
  assert.equal(flushed.length, 0);
  assert.equal(timers.filter(t => t.live).length, 1);
  assert.equal(timers.at(-1).ms, 2000);
  timers.at(-1).fn();
  assert.deepEqual(flushed, [{ a: R(112, 100, 50, 50) }]);
  coalescer.stage({ b: R(1, 2, 30, 30) }, "now");
  assert.equal(flushed.length, 2);
  assert.deepEqual(flushed[1], { b: R(1, 2, 30, 30) });
  // Esc drops what has not been sent and says which visuals to roll back.
  coalescer.stage({ c: R(0, 0, 10, 10) }, "idle");
  assert.deepEqual(coalescer.cancel(), ["c"]);
  assert.equal(coalescer.hasPending(), false);
  assert.equal(timers.at(-1).live, false);
});

test("optimistic rects show over the server's and roll back on failure", () => {
  const items = [{ id: "a", ...R(1, 1, 10, 10), title: "A" }, { id: "b", ...R(2, 2, 10, 10), title: "B" }];
  const override = { a: R(50, 60, 10, 10) };
  assert.deepEqual(geometry.withOverride(items, override).map(i => [i.id, i.x, i.y]), [["a", 50, 60], ["b", 2, 2]]);
  const rolledBack = geometry.dropOverride(override, ["a"]);
  assert.deepEqual(rolledBack, {});
  assert.deepEqual(geometry.withOverride(items, rolledBack), items);
  assert.deepEqual(override, { a: R(50, 60, 10, 10) });
});

test("clicking picks the top-most visible visual; groups are skipped unless Alt is held", () => {
  const rects = [
    { id: "back", x: 0, y: 0, width: 500, height: 500, rank: 1 },
    { id: "grp", x: 100, y: 100, width: 300, height: 300, rank: 2, kind: "group" },
    { id: "kid", x: 120, y: 120, width: 100, height: 100, rank: 3, parentGroup: "grp" },
    { id: "ghost", x: 120, y: 120, width: 100, height: 100, rank: 9, hidden: true },
  ];
  assert.equal(geometry.hitTest(rects, 150, 150), "kid");
  assert.equal(geometry.hitTest(rects, 150, 150, { alt: true }), "grp");
  assert.equal(geometry.hitTest(rects, 350, 350), "back");
  assert.equal(geometry.hitTest(rects, 600, 600), null);
});

test("align and distribute", () => {
  const rects = { a: R(0, 0, 100, 40), b: R(200, 50, 50, 40), c: R(400, 100, 100, 40) };
  assert.deepEqual(geometry.alignRects(rects, ["a", "b", "c"], "left"), { a: R(0, 0, 100, 40), b: R(0, 50, 50, 40), c: R(0, 100, 100, 40) });
  assert.deepEqual(geometry.alignRects(rects, ["a", "b", "c"], "right").b, R(450, 50, 50, 40));
  assert.deepEqual(geometry.alignRects(rects, ["a", "b", "c"], "center").b, R(225, 50, 50, 40));
  assert.deepEqual(geometry.alignRects(rects, ["a"], "left"), {});
  assert.deepEqual(geometry.distributeRects(rects, ["a", "b", "c"], "horizontal").b, R(225, 50, 50, 40));
  assert.deepEqual(geometry.distributeRects(rects, ["a", "b"], "horizontal"), {});
});

test("requests run one at a time, in order", async () => {
  const queue = new geometry.SerialQueue();
  const order = [];
  const slow = queue.run(async () => { order.push("a-start"); await new Promise(r => setTimeout(r, 20)); order.push("a-end"); return 1; });
  const fast = queue.run(async () => { order.push("b"); return 2; });
  assert.equal(queue.busy, true);
  assert.deepEqual(await Promise.all([slow, fast]), [1, 2]);
  assert.deepEqual(order, ["a-start", "a-end", "b"]);
  const failing = queue.run(async () => { throw new Error("no"); });
  const after = queue.run(async () => "still runs");
  await assert.rejects(failing);
  assert.equal(await after, "still runs");
  assert.equal(queue.busy, false);
});

// --- the Visual pane -------------------------------------------------------------------------------------------------------------
// Studio does not change styling: whatever the report's formatting and whatever the catalog lists, the pane only ever
// sends the operations below.
const PANE_OPS = new Set(["set_number_format", "set_table_layout", "move_column", "set_field_display_name", "set_position", "arrange", "set_visual_hidden"]);

const chart = () => ({
  id: "b404", kind: "column", visualType: "clusteredColumnChart", rect: R(20, 30, 400, 240),
  formatting: {
    title: { show: true, text: "Sales by Region", textSource: "automatic", automaticText: "Sales by Region", fontSize: 16, fontColor: "#1F2933", fontFamily: "\"Segoe UI Semibold\", wf_segoe-ui_semibold, helvetica", bold: false, italic: false, alignment: "left", background: null },
    container: { background: "#ff880099", border: { show: true, color: "#123456", width: 2, radius: 8 }, shadow: false, padding: [5, null, null, 7] },
    labels: { show: true, fontSize: 16, color: "#252423", displayUnits: 1000, decimals: 1, position: "OutsideEnd" },
    legend: { show: true, position: "top", alignment: "center", fontSize: 13.33, color: "#252423" },
    axes: { category: { show: true, title: false, gridlines: false, fontSize: 13.33, color: "#000000" }, value: { show: true, title: true, gridlines: true, fontSize: 12, color: "#111111" } },
    series: { defaultColor: "#1E90FF", seriesColors: { "Orders.Sales": "#0B6E4F" }, categoryColors: [{ value: "Others", color: "#cccccc", column: "Orders.Region" }], varyByCategory: false },
  },
  columns: [
    { kind: "column", table: "Orders", name: "Region", label: "Region", role: "Category", query_ref: "Orders.Region" },
    { kind: "measure", table: "Orders", name: "Sales", label: "Sales", role: "Y", query_ref: "Orders.Sales" },
    { kind: "measure", table: "Orders", name: "Fmt", label: "Fmt", role: "Formatting", query_ref: "Orders.Fmt", hidden: true },
  ],
  rows: [["West", 5], ["Others", 2], ["West", 7], [null, 1], ["East", 3]],
  unrendered: ["categoryAxis.innerPadding"],
});
const control = (sections, id) => sections.flatMap(s => s.controls).find(c => c.id === id);

test("without a catalog the pane offers number format and size & position, never styling", () => {
  const sections = format.curatedSections(chart());
  assert.deepEqual(sections.map(s => s.title), ["Number format", "Size & position"]);
  assert.deepEqual(format.curatedSections({ id: "g", kind: "group", visualType: "visualGroup" }), []);
  assert.deepEqual(format.curatedSections({ id: "t", kind: "textbox", visualType: "textbox" }).map(s => s.title), ["Size & position"]);
  assert.deepEqual(format.curatedSections({ id: "c", kind: "card", visualType: "cardVisual" }).map(s => s.title), ["Number format", "Size & position"]);
  assert.deepEqual(format.curatedSections({ id: "tb", kind: "table", visualType: "tableEx" }).map(s => s.title), ["Size & position"]);
});

test("the pane's own controls read the renderer's tokens and send number formats and positions only", () => {
  const info = chart(); const sections = format.curatedSections(info);
  assert.equal(control(sections, "number.units").read(info), "thousands");
  assert.equal(control(sections, "number.decimals").read(info), 1);
  assert.equal(control(sections, "position.width").read(info), 400);
  const write = (id, value) => control(sections, id).write(info, value);
  assert.deepEqual(write("number.units", "millions"), { op: "set_number_format", visual: "b404", target: "data_labels", display_units: "millions" });
  assert.deepEqual(write("number.decimals", 2), { op: "set_number_format", visual: "b404", target: "data_labels", decimals: 2 });
  assert.deepEqual(write("position.x", 40), { op: "set_position", visual: "b404", x: 40 });
  assert.equal(write("number.decimals", "two"), undefined);
  // A card's value takes its number format through the same operation.
  const card = { id: "k", kind: "card", visualType: "cardVisual", formatting: { cardValue: { displayUnits: 1000000, decimals: 2 } } };
  const cardSections = format.curatedSections(card);
  assert.equal(control(cardSections, "number.units").read(card), "millions");
  assert.deepEqual(control(cardSections, "number.units").props, ["value.labelDisplayUnits"]);
  assert.deepEqual(control(cardSections, "number.units").write(card, "thousands"), { op: "set_number_format", visual: "k", target: "card_value", display_units: "thousands" });
  // Every control of every kind writes an operation the pane may send.
  for (const kind of ["column", "bar", "line", "area", "pie", "donut", "card", "table", "matrix", "slicer", "textbox", "shape", "button", "image"]) {
    const info = { id: "v", kind, visualType: kind, rect: R(0, 0, 100, 100) };
    for (const spec of format.curatedSections(info).flatMap(s => s.controls)) {
      const value = spec.input === "select" ? spec.options[0].value : 10;
      const op = spec.write(info, value);
      assert.ok(op && PANE_OPS.has(op.op), `${kind} ${spec.id}`);
    }
  }
});

// An offline answer of GET .../format-catalog (imported_format_catalog.format_catalog), for two visual types.
const fixture = JSON.parse(readFileSync(new URL("./imported-format-catalog.fixture.json", import.meta.url), "utf8"));
const chartCatalog = () => format.readFormatCatalog(fixture.clusteredColumnChart, "clusteredColumnChart");
// A stale server answer must never bring removed styling controls back into the pane.
const staleStylingCatalog = () => format.readFormatCatalog({ ...fixture.clusteredColumnChart, sections: [
  ...fixture.clusteredColumnChart.sections,
  { id: "caption", title: "Caption", controls: [{ id: "title.text", object: "title", property: "text", kind: "text", op: { name: "set_title", field: "text" } }] },
  { id: "colors", title: "Colors", controls: [{ id: "dataPoint.defaultColor", object: "dataPoint", property: "defaultColor", kind: "color", op: { name: "set_colors", field: "color" } }] },
  { id: "background_border", title: "Background & border", controls: [{ id: "background.show", object: "background", property: "show", kind: "toggle", op: { name: "set_visual_property", field: "value", fixed: { object: "background", property: "show" } } }] },
] }, "clusteredColumnChart");
// A table's catalog: number format, columns, table layout and size & position, with a few controls an older server also listed.
const tableCatalog = () => format.readFormatCatalog({ visual_type: "tableEx", sections: [
  { id: "caption", title: "Caption", controls: [{ id: "title.text", label: "Title text", object: "title", property: "text", kind: "text", op: { name: "set_title", field: "text" } }] },
  { id: "size_position", title: "Size & position", controls: [
    { id: "position.x", label: "Left", object: "position", property: "x", kind: "number", unit: "px", op: { name: "set_position", field: "x" } },
    { id: "position.hidden", label: "Hidden", object: "position", property: "hidden", kind: "toggle", op: { name: "set_visual_hidden", field: "hidden" } },
  ] },
  { id: "number_format", title: "Number format", controls: [
    { id: "numberFormat.code", label: "Format code", object: "query", property: "format", kind: "text", theme_default: "#,0", op: { name: "set_number_format", field: "format", fixed: { target: "column" } } },
    { id: "columnFormatting.labelDisplayUnits", label: "Display units", object: "columnFormatting", property: "labelDisplayUnits", kind: "select", options: [{ value: "0", label: "Auto" }], op: { name: "set_visual_property", field: "value", fixed: { object: "columnFormatting", property: "labelDisplayUnits" } } },
  ] },
  { id: "conditional_formatting", title: "Conditional formatting", controls: [{ id: "cf.fontColor", label: "Lowest color", object: "values", property: "fontColor", kind: "color", op: { name: "set_conditional_format", field: "min_color", fixed: { target: "font_color" } } }] },
  { id: "columns", title: "Columns", controls: [
    { id: "columns.headerText", label: "Header text", object: "query", property: "displayName", kind: "text", writable: false, scope: "field", op: { name: "set_field_display_name", field: "name" } },
    { id: "columns.width", label: "Width", object: "columnWidth", property: "value", kind: "number", writable: false, scope: "field", op: { name: "set_column_format", field: "width" } },
    { id: "columns.position", label: "Position", object: "query", property: "order", kind: "number", writable: false, scope: "field", op: { name: "move_column", field: "to_index" } },
  ] },
  { id: "table_layout", title: "Table layout", controls: [
    { id: "tableLayout.totals", label: "Totals", object: "total", property: "totals", kind: "toggle", op: { name: "set_table_layout", field: "totals" } },
    { id: "tableLayout.bandedRows", label: "Banded rows", object: "values", property: "backColorSecondary", kind: "toggle", op: { name: "set_table_layout", field: "banded_rows" } },
    { id: "tableLayout.bandColor", label: "Band color", object: "values", property: "backColorSecondary", kind: "color", op: { name: "set_table_layout", field: "band_color" } },
    { id: "tableLayout.wordWrap", label: "Wrap values", object: "values", property: "wordWrap", kind: "toggle", op: { name: "set_table_layout", field: "word_wrap" } },
  ] },
] }, "tableEx");

test("the catalog answer is read section by section, with the operation each control writes", () => {
  const catalog = chartCatalog();
  assert.equal(catalog.studioDraws, true);
  // The reader keeps the answer as it came; what the pane offers is decided by `sectionsFor` (below).
  assert.deepEqual(catalog.sections.map(s => s.title), ["Size & position", "Number format", "Conditional formatting"]);
  const z = catalog.sections.find(s => s.id === "size_position").controls.find(c => c.id === "position.z");
  assert.equal(z.kind, "select");
  assert.deepEqual(z.op, { name: "arrange", field: "to", fixed: {} });
  assert.equal("generic_op" in z, false);
  assert.equal("properties" in catalog, false);
  assert.equal(catalog.roles.length, 0);
  // Malformed pieces are dropped, not trusted.
  const rough = format.readFormatCatalog({ sections: [{ id: "x", title: "X", controls: [{ id: "a" }, { object: "o", property: "p", kind: "weird", op: { name: "set_position", field: "x" } }, "junk"] }, { id: "empty", controls: [] }] }, "t");
  assert.deepEqual(rough.sections.map(s => s.controls.map(c => [c.id, c.kind])), [[["o.p", "text"]]]);
  assert.equal(format.readFormatCatalog(null, "z").visualType, "z");
});

test("a stale catalog cannot restore styling controls, while number formats and positions remain", () => {
  const info = chart();
  const catalog = staleStylingCatalog();
  const sections = format.sectionsFor(info, catalog);
  assert.deepEqual(sections.map(s => s.title), ["Number format", "Size & position"]);
  const find = id => sections.flatMap(s => s.controls).find(c => c.id === id);
  const send = (id, value) => find(id).write(info, value);
  assert.equal(find("position.width").read(info), 400);
  assert.equal(find("position.hidden").read({ ...info, hidden: true }), true);
  assert.deepEqual(send("position.z", "front"), { op: "arrange", visual: "b404", to: "front" });
  assert.deepEqual(send("position.hidden", true), { op: "set_visual_hidden", visual: "b404", hidden: true });
  assert.deepEqual(send("position.x", 40), { op: "set_position", visual: "b404", x: 40 });
  assert.equal(send("position.x", undefined), undefined);
  // Titles, colors, data labels, legend, axes, background and border, and the generic property route: all left out.
  const offered = new Set(sections.flatMap(s => s.controls).map(c => c.id));
  const styling = catalog.sections.flatMap(s => s.controls).filter(c => !PANE_OPS.has(c.op.name));
  assert.equal(styling.length, 8);
  assert.ok(styling.every(c => !offered.has(c.id) && !format.isPaneControl(c)));
  const card = { id: "k", kind: "card", visualType: "cardVisual", formatting: { cardValue: { fontSize: 60, color: "#123456", displayUnits: 1000000, decimals: 2 }, cardLabel: { text: "Agents", show: true } } };
  assert.deepEqual(format.sectionsFor(card, format.readFormatCatalog(fixture.cardVisual, "cardVisual")).map(s => s.title), ["Number format", "Size & position"]);
});

test("catalog number formats read and write the operation's display-unit words", () => {
  const info = { ...chart(), formatting: { ...chart().formatting, axes: { value: { displayUnits: 1000000, decimals: 2 } }, labels: { displayUnits: 1000, decimals: 1, format: "0.0%" } } };
  const sections = format.sectionsFor(info, chartCatalog());
  assert.equal(control(sections, "labels.labelDisplayUnits").read(info), "thousands");
  assert.equal(control(sections, "valueAxis.labelDisplayUnits").read(info), "millions");
  assert.equal(control(sections, "valueAxis.labelPrecision").read(info), 2);
  assert.equal(control(sections, "numberFormat.code").read(info), "0.0%");
  assert.equal(control(sections, "numberFormat.preset").read(info), undefined, "a display unit alone does not identify a preset");
  for (const value of ["auto", "none", "thousands", "millions", "billions", "trillions"]) {
    assert.deepEqual(control(sections, "labels.labelDisplayUnits").write(info, value), { op: "set_number_format", visual: "b404", target: "data_labels", display_units: value });
  }
  const card = { id: "k", kind: "card", visualType: "cardVisual", formatting: { cardValue: { displayUnits: 1e12, decimals: 2, format: "$#,0.00" } } };
  const cardSections = format.sectionsFor(card, format.readFormatCatalog(fixture.cardVisual, "cardVisual"));
  assert.equal(control(cardSections, "value.labelDisplayUnits").read(card), "trillions");
  assert.equal(control(cardSections, "numberFormat.code").read(card), "$#,0.00");
});

test("the pane keeps number formats, the table's structure and size & position, in that order", () => {
  const info = { id: "t1", kind: "table", visualType: "tableEx", rect: R(0, 0, 400, 240), columns: [
    { kind: "column", table: "Orders", name: "Region", label: "Region", role: "Values", query_ref: "Orders.Region" },
    { kind: "measure", table: "Orders", name: "Sales", label: "Sales", role: "Values", query_ref: "Orders.Sales" },
  ] };
  const sections = format.sectionsFor(info, tableCatalog());
  assert.deepEqual(sections.map(s => [s.title, s.controls.map(c => c.id)]), [
    ["Number format", ["numberFormat.code"]],
    ["Columns", ["columns.headerText", "columns.position"]],
    ["Table layout", ["tableLayout.totals"]],
    ["Size & position", ["position.x", "position.hidden"]],
  ]);
  const find = id => sections.flatMap(s => s.controls).find(c => c.id === id);
  assert.deepEqual(find("numberFormat.code").write(info, "0.0%"), { op: "set_number_format", visual: "t1", target: "column", format: "0.0%" });
  assert.equal(find("numberFormat.code").themeDefault, "#,0");
  assert.equal(find("numberFormat.code").placeholder, "Auto (#,0)");
  assert.deepEqual(find("tableLayout.totals").write(info, false), { op: "set_table_layout", visual: "t1", totals: false });
  assert.deepEqual(find("columns.position").write(info, 0, "Sales"), { op: "move_column", visual: "t1", field: "Sales", to_index: 0 });
  assert.equal(find("columns.position").write(info, 0, undefined), undefined);
  assert.equal(find("columns.headerText").read(info, "Sales"), "Sales");
  // Table layout: the structural fields only, never with a styling field fixed in; never a color; never a styling operation.
  const control = (kind, name, field, fixed = {}) => ({ kind, op: { name, field, fixed } });
  for (const field of ["totals", "row_subtotals", "column_subtotals", "stepped", "layout", "values_on_rows"]) assert.equal(format.isPaneControl(control("toggle", "set_table_layout", field)), true, field);
  for (const field of ["banded_rows", "band_color", "grid_horizontal", "grid_vertical", "row_padding", "word_wrap", "header_word_wrap"]) assert.equal(format.isPaneControl(control("toggle", "set_table_layout", field)), false, field);
  assert.equal(format.isPaneControl(control("toggle", "set_table_layout", "totals", { word_wrap: true })), false);
  assert.equal(format.isPaneControl(control("color", "set_number_format", "format")), false);
  for (const name of ["set_visual_property", "set_title", "set_colors", "set_font", "set_data_labels", "set_legend", "set_axis", "set_container", "set_column_format"]) {
    assert.equal(format.isPaneControl(control("text", name, "value")), false, name);
  }
});

test("Auto (theme) hints and the Shown in Power BI tag", () => {
  const info = chart();
  assert.equal(format.isAuto(12, 12), true);
  assert.equal(format.isAuto(12.004, 12), true);
  assert.equal(format.isAuto(14, 12), false);
  assert.equal(format.isAuto("#ABCDEF", "#abcdef"), true);
  assert.equal(format.isAuto(undefined, undefined), true);
  assert.equal(format.isAuto(true, undefined), false);
  assert.equal(format.isAuto(false, null), false);
  // Properties the catalog says Studio's canvas does not draw carry the tag; so do the ones the visual's own row lists as not drawn.
  assert.equal(format.shownInPowerBiOnly({ props: ["labels.labelDisplayUnits"], studioRender: false }, info), true);
  assert.equal(format.shownInPowerBiOnly({ props: ["categoryAxis.innerPadding"] }, info), true);
  assert.equal(format.shownInPowerBiOnly({ props: ["labels.labelDisplayUnits"] }, info), false);
  assert.equal(format.shownInPowerBiOnly({ props: [] }, info), false);
});

test("field wells follow the catalog's roles, and the field picker lists columns and measures", () => {
  const info = chart();
  const catalog = format.readFormatCatalog({ roles: [{ role: "Category", kind: "Grouping", label: "X-axis" }, { role: "Series", kind: "Grouping", label: "Legend" }, { role: "Y", kind: "Measure", label: "Y-axis" }] }, "x");
  const wells = format.fieldWells("clusteredColumnChart", catalog, info.columns);
  assert.deepEqual(wells.map(w => [w.role.label, w.fields.map(f => f.name)]), [["X-axis", ["Region"]], ["Legend", []], ["Y-axis", ["Sales"]]]);
  // The real catalog answer has no roles yet: the built-in table of the addable types stands in, in Power BI's order.
  const builtIn = format.fieldWells("clusteredColumnChart", chartCatalog(), info.columns);
  assert.deepEqual(builtIn.map(w => [w.role.label, w.role.max ?? null, w.fields.map(f => f.name)]), [["X-axis", null, ["Region"]], ["Legend", 1, []], ["Y-axis", null, ["Sales"]], ["Small multiples", null, []]]);
  assert.deepEqual(format.fieldWells("pieChart", undefined, []).map(w => w.role.label), ["Legend", "Details", "Values"]);
  assert.deepEqual(format.fieldWells("mapVisual", undefined, info.columns).map(w => w.role.role), ["Category", "Y"]);
  assert.deepEqual(format.rolesFor("kpi", undefined).map(r => [r.role, r.kind, r.max ?? null]), [["Indicator", "Measure", 1], ["TrendLine", "Grouping", 1], ["Goal", "Measure", 2]]);
  assert.equal(format.ADDABLE_TYPES.every(t => format.rolesFor(t.type, undefined).length > 0), true);
  const choices = format.fieldChoices([{ TableName: "Orders", ColumnName: "Region", PandasDataType: "object" }, { TableName: "Orders", ColumnName: "Qty", PandasDataType: "int64" }], [{ TableName: "# Measures", Name: "Total Sales" }]);
  assert.deepEqual(choices.map(c => [c.name, c.kind, c.numeric]), [["Region", "column", false], ["Qty", "column", true], ["Total Sales", "measure", true]]);
  assert.deepEqual(format.fieldRef(choices[1], "Average"), { table: "Orders", name: "Qty", aggregation: "Average" });
  assert.deepEqual(format.fieldRef(choices[0]), { table: "Orders", name: "Region" });
  assert.equal(format.ADDABLE_TYPES.length, 25);
  assert.equal(format.visualTypeLabel("clusteredColumnChart"), "Clustered column chart");
  assert.equal(format.visualTypeLabel("mapVisual"), "Map Visual");
});

// --- driving a job through conflicts -------------------------------------------------------------------------------------------

const drive = (transportScript, job, decisions, extra = {}) => {
  const s = script(...transportScript);
  const client = createEditingClient(s.transport, options());
  const asked = [];
  let head = extra.head ?? "r0007";
  const io = {
    head: () => head, newKey: () => `fresh-${asked.length}-${s.calls.length}`,
    decide: async problem => { asked.push(problem.kind); const next = decisions.shift(); return typeof next === "function" ? next(problem) : next ?? { action: "dismiss" }; },
    refreshHead: async actual => { head = actual ?? "r0009"; return head; },
  };
  return editing.driveJob(client, "w", job, io).then(outcome => ({ outcome, calls: s.calls, asked, head: () => head }));
};
const editJob = () => ({ kind: "edit", key: "job-key-0001", operations: [{ op: "set_number_format", visual: "v", preset: "percent" }], options: {} });

test("unsaved Desktop work: Save into history retries the same key, Cancel changes nothing", async () => {
  const saved = await drive([{ status: 409, body: { code: "desktop_unsaved" } }, { status: 200, body: receiptBody("r0008") }], editJob(), [{ action: "save_and_continue" }]);
  assert.equal(saved.outcome.status, "applied");
  assert.deepEqual(saved.asked, ["desktop_unsaved"]);
  assert.equal(saved.calls[1].body.idempotency_key, "job-key-0001");
  assert.equal(saved.calls[1].body.desktop_work, "save");
  const cancelled = await drive([{ status: 409, body: { code: "desktop_unsaved" } }], editJob(), [{ action: "cancel" }]);
  assert.deepEqual(cancelled.outcome, { status: "cancelled" });
  assert.equal(cancelled.calls.length, 1);
});

test("a moved head refreshes and offers Redo my change on the new head with a fresh key", async () => {
  const run = await drive([{ status: 409, body: { code: "head_moved", expected: "r0007", actual: "r0008" } }, { status: 200, body: receiptBody("r0009") }], editJob(), [{ action: "redo" }]);
  assert.equal(run.outcome.status, "applied");
  assert.equal(run.calls[1].body.expected_head, "r0008");
  assert.notEqual(run.calls[1].body.idempotency_key, run.calls[0].body.idempotency_key);
  assert.deepEqual(run.calls[1].body.operations, run.calls[0].body.operations);
  const dismissed = await drive([{ status: 409, body: { code: "head_moved", actual: "r0008" } }], editJob(), []);
  assert.equal(dismissed.outcome.status, "failed");
  // Undo never re-aims itself at a newer head: it could undo a different change than the one the user saw.
  const undo = await drive([{ status: 409, body: { code: "head_moved", actual: "r0008" } }], { kind: "undo", key: "u-key-0001", options: {} }, [{ action: "redo" }]);
  assert.equal(undo.outcome.status, "failed");
  assert.equal(undo.calls.length, 1);
});

test("replacing conditional formatting retries with confirm_replace; Keep sends nothing more", async () => {
  const detail = "This replaces conditional formatting on Agent ID.";
  const replaced = await drive([{ status: 409, body: { code: "needs_confirmation", kind: "replace_conditional", detail } }, { status: 200, body: receiptBody("r0008") }], editJob(), [{ action: "replace" }]);
  assert.equal(replaced.outcome.status, "applied");
  assert.equal(replaced.calls[1].body.confirm_replace, true);
  assert.equal(replaced.calls[1].body.confirm_model, undefined);
  const kept = await drive([{ status: 409, body: { code: "needs_confirmation", kind: "replace_conditional", detail } }], editJob(), [{ action: "keep" }]);
  assert.deepEqual(kept.outcome, { status: "cancelled" });
  const model = await drive([{ status: 409, body: { code: "needs_confirmation", kind: "model_edit" } }, { status: 200, body: receiptBody("r0008") }], editJob(), [{ action: "replace" }]);
  assert.equal(model.calls[1].body.confirm_model, true);
  const restore = await drive([{ status: 409, body: { code: "needs_confirmation", kind: "restore_discard" } }, { status: 200, body: receiptBody("r0008") }], { kind: "restore", key: "r-key-0001", revisionId: "r0003", confirmedDiscard: false, options: {} }, [{ action: "replace" }]);
  assert.equal(restore.calls[1].body.confirmed_discard, true);
  assert.equal(restore.calls[1].body.revision_id, "r0003");
});

test("an answered confirmation is written into the operations the server reads it from", async () => {
  const conditional = { status: 409, body: { code: "needs_confirmation", kind: "replace_conditional", detail: "This replaces conditional formatting on Agent ID." } };
  // The server reads `replace_conditional` on each operation that declares it, not the request-level flag.
  const numbers = await drive([conditional, { status: 200, body: receiptBody("r0008") }],
    { kind: "edit", key: "c-key-00001", operations: [{ op: "set_number_format", visual: "v", preset: "percent" }, { op: "set_position", visual: "v", x: 1, y: 2, width: 3, height: 4 }], options: {} }, [{ action: "replace" }]);
  assert.equal(numbers.outcome.status, "applied");
  assert.deepEqual(numbers.calls[1].body.operations, [{ op: "set_number_format", visual: "v", preset: "percent", replace_conditional: true }, { op: "set_position", visual: "v", x: 1, y: 2, width: 3, height: 4 }]);
  assert.equal(numbers.calls[1].body.confirm_replace, true);
  assert.notEqual(numbers.calls[1].body.idempotency_key, "c-key-00001");
  assert.deepEqual(editing.confirmOperations([{ op: "set_table_layout", visual: "v", totals: true }, { op: "set_alt_text", visual: "v", text: "x" }], "replace_conditional"),
    [{ op: "set_table_layout", visual: "v", totals: true, replace_conditional: true }, { op: "set_alt_text", visual: "v", text: "x" }]);
  // Deleting a page that buttons or bookmarks point at needs `cleanup_references` on the delete only.
  const pages = { status: 409, body: { code: "needs_confirmation", kind: "remove_page_references", detail: "2 buttons and 1 bookmark point at this page." } };
  const removed = await drive([pages, { status: 200, body: receiptBody("r0008") }],
    { kind: "edit", key: "d-key-00001", operations: [{ op: "rename_page", page: "p2", name: "B" }, { op: "delete_page", page: "p1" }], options: {} }, [{ action: "replace" }]);
  assert.equal(removed.outcome.status, "applied");
  assert.deepEqual(removed.calls[1].body.operations, [{ op: "rename_page", page: "p2", name: "B" }, { op: "delete_page", page: "p1", cleanup_references: true }]);
  // Answers that need no operation flag leave the operations alone.
  assert.deepEqual(editing.confirmOperations([{ op: "create_measure", name: "M" }], "model_edit"), [{ op: "create_measure", name: "M" }]);
  // Both answers in a row: the flag written for the first stays for the second.
  const both = await drive([conditional, pages, { status: 200, body: receiptBody("r0008") }],
    { kind: "edit", key: "b-key-00001", operations: [{ op: "set_conditional_format", visual: "v", style: "gradient" }, { op: "delete_page", page: "p1" }], options: {} }, [{ action: "replace" }, { action: "replace" }]);
  assert.deepEqual(both.calls[2].body.operations, [{ op: "set_conditional_format", visual: "v", style: "gradient", replace_conditional: true }, { op: "delete_page", page: "p1", cleanup_references: true }]);
});

test("an Undo or restore that meets a moved head offers only Close, an edit offers Redo", async () => {
  const moved = classifyEditProblem(409, { code: "head_moved", actual: "r0008" });
  assert.deepEqual(problemUi(moved).actions, ["redo", "dismiss"]);
  assert.deepEqual(problemUi(moved, "edit").actions, ["redo", "dismiss"]);
  for (const kind of ["undo", "restore"]) {
    const ui = problemUi(moved, kind);
    assert.deepEqual(ui.actions, ["dismiss"]);
    assert.match(ui.body, /Look at History and try again/);
  }
  // driveJob hands the job's kind to the dialog.
  const kinds = [];
  const s = script({ status: 409, body: { code: "head_moved", actual: "r0008" } });
  await editing.driveJob(createEditingClient(s.transport, options()), "w", { kind: "undo", key: "u-key-0002", options: {} },
    { head: () => "r0007", refreshHead: async () => "r0008", decide: async (_problem, kind) => { kinds.push(kind); return { action: "dismiss" }; } });
  assert.deepEqual(kinds, ["undo"]);
});

test("Restore to before this names the row that was clicked", () => {
  const later = [{ author: "studio", summary: "Made bars green", created: "2026-09-28T10:00:00Z" }];
  assert.equal(restorePrompt({ summary: "Moved KPI" }, later, "Made bars green").title, "Go back to before “Made bars green”?");
  assert.equal(restorePrompt({ summary: "Moved KPI" }, later).title, "Go back to before “Moved KPI”?");
});

test("a Desktop cycle started in one tab is the busy state of the other tab of the same report", () => {
  const store = editing.createBusyStore();
  let notified = 0;
  const off = store.subscribe(() => { notified += 1; });
  assert.equal(store.get("ws"), null);
  const busy = { label: "Applying your changes in Power BI", desktop: true };
  store.set("ws", "model-tab", busy);
  assert.equal(store.get("ws"), busy);
  assert.equal(store.get("other"), null);
  assert.equal(notified, 1);
  store.set("ws", "model-tab", busy);
  assert.equal(notified, 1, "an unchanged value is not announced again");
  store.set("ws", "report-tab", { label: "Moving the visual", desktop: false });
  assert.equal(store.get("ws"), busy, "the first running change stays the one shown");
  store.set("ws", "model-tab", null);
  assert.equal(store.get("ws").label, "Moving the visual");
  store.set("ws", "report-tab", null);
  assert.equal(store.get("ws"), null);
  assert.equal(notified, 3, "announced when the first change starts, when it hands over, and when the last one ends");
  off();
  store.set("ws", "model-tab", busy);
  assert.equal(notified, 3);
});

test("the DAX a chat confirmation shows is each operation's formula, named by what it does", () => {
  assert.deepEqual(editing.operationFormulas([
    { op: "create_measure", table: "Sales", name: "Margin", expression: "  DIVIDE([Profit], [Sales])  " },
    { op: "rename_measure", name: "A", new_name: "B" },
    { op: "update_measure", name: "Sales", expression: "SUM(Sales[Amount])", format_string: "#,0" },
  ]), [
    { title: "Add the measure Margin to Sales", expression: "DIVIDE([Profit], [Sales])" },
    { title: "Change the formula and the number format of Sales", expression: "SUM(Sales[Amount])" },
  ]);
  assert.deepEqual(editing.operationFormulas([]), []);
});

test("a choice resends the operation with the answer's fill; a choice without one sends nothing", async () => {
  const job = () => ({ kind: "edit", key: "t-key-00001", operations: [{ op: "set_conditional_format", visual: "t", style: "gradient", field: "Sales" }], options: {} });
  const question = "Color the text or the cell background?";
  const run = await drive([
    { status: 409, body: { code: "needs_choice", question, message: question, choices: [{ index: 0, label: "The text (font color)", fill: { target: "font_color" } }, { index: 1, label: "The cell background", fill: { target: "background" } }] } },
    { status: 200, body: receiptBody("r0008") },
  ], job(), [{ choice: "1" }]);
  assert.equal(run.outcome.status, "applied");
  assert.deepEqual(run.calls[1].body.operations, [{ op: "set_conditional_format", visual: "t", style: "gradient", field: "Sales", target: "background" }]);
  assert.notEqual(run.calls[1].body.idempotency_key, "t-key-00001");
  // The dialog offers the server's labels, with the index as each choice's value.
  const asked = classifyEditProblem(409, { code: "needs_choice", question, choices: [{ index: 0, label: "The text", fill: { target: "font_color" } }] });
  assert.deepEqual(asked.choices, [{ value: "0", label: "The text", fill: { target: "font_color" } }]);
  // A choice without a fill cannot be written into the operations: nothing more is sent.
  const plain = await drive([{ status: 409, body: { code: "needs_choice", question, choices: [{ value: "text", label: "The text" }] } }], job(), [{ choice: "text" }]);
  assert.deepEqual(plain.outcome, { status: "cancelled" });
  assert.equal(plain.calls.length, 1);
  // The answer goes into the first operation of the batch.
  assert.deepEqual(editing.fillOperations([{ op: "set_conditional_format", visual: "a" }, { op: "set_position", visual: "b" }], { field: "Sales" }), [{ op: "set_conditional_format", visual: "a", field: "Sales" }, { op: "set_position", visual: "b" }]);
  assert.equal(classifyEditProblem(409, { code: "desktop_busy", message: "Power BI is busy." }).kind, "busy");
  assert.equal(classifyEditProblem(409, { code: "needs_confirmation", kind: "remove_page_references", detail: "2 buttons and 1 bookmark point at this page." }).confirm, "delete_page");
  assert.equal(problemUi(classifyEditProblem(409, { code: "needs_confirmation", kind: "remove_page_references", detail: "2 buttons and 1 bookmark point at this page." })).body, "2 buttons and 1 bookmark point at this page.");
});

test("busy and dialog problems retry with the same key; a rejection is shown once and fails", async () => {
  const busy = await drive([{ status: 409, body: { code: "edit_busy" } }, { status: 200, body: receiptBody("r0008") }], editJob(), [{ action: "retry" }]);
  assert.equal(busy.outcome.status, "applied");
  assert.equal(busy.calls[1].body.idempotency_key, busy.calls[0].body.idempotency_key);
  const rejected = await drive([{ status: 422, body: { code: "edit_rejected", message: "A card has no totals." } }], editJob(), []);
  assert.deepEqual(rejected.outcome, { status: "failed", problem: { kind: "rejected", message: "A card has no totals.", step: undefined } });
  assert.deepEqual(rejected.asked, ["rejected"]);
  const unavailable = await drive([{ status: 503, body: { code: "desktop_unavailable", message: "Power BI Desktop is not installed." } }], editJob(), []);
  assert.equal(unavailable.outcome.problem.kind, "desktop_unavailable");
  // A run of conflicts ends instead of looping forever.
  const loop = await drive(Array.from({ length: 12 }, () => ({ status: 409, body: { code: "edit_busy" } })), editJob(), Array.from({ length: 12 }, () => ({ action: "retry" })));
  assert.equal(loop.outcome.status, "failed");
});

test("the head only moves forward when a payload and a receipt race", () => {
  const h = (id, hash = id) => ({ id, hash });
  assert.deepEqual(editing.newerHead(h("r0008"), h("r0007")), h("r0008"));
  assert.deepEqual(editing.newerHead(h("r0008"), h("r0009")), h("r0009"));
  assert.deepEqual(editing.newerHead(undefined, h("r0001")), h("r0001"));
  assert.deepEqual(editing.newerHead(h("r0008"), undefined), h("r0008"));
  assert.deepEqual(editing.newerHead(h("abc"), h("def")), h("def"));
});

test("a new visual is placed in the first free spot on the page", () => {
  const page = { width: 1280, height: 720 };
  assert.deepEqual(geometry.findFreeSpot([], { width: 400, height: 250 }, page), { x: 20, y: 20 });
  assert.deepEqual(geometry.findFreeSpot([R(0, 0, 500, 300)], { width: 400, height: 250 }, page), { x: 500, y: 20 });
  assert.deepEqual(geometry.findFreeSpot([R(0, 0, 1280, 300)], { width: 400, height: 250 }, page), { x: 20, y: 300 });
  assert.deepEqual(geometry.findFreeSpot([R(0, 0, 1280, 720)], { width: 400, height: 250 }, page), { x: 20, y: 20 });
});

test("measure edits send only what changed and check the name", () => {
  const original = { TableName: "Sales", Name: "Margin", Expression: "DIVIDE([Profit],[Sales])", FormatString: "0.0%", Description: "Profit share" };
  assert.equal(editing.measureUpdateOp(original, { expression: " DIVIDE([Profit],[Sales]) ", format: "0.0%", description: "Profit share" }), undefined);
  assert.deepEqual(editing.measureUpdateOp(original, { expression: "DIVIDE([Profit],[Sales],0)", format: "0.0%", description: "Profit share" }), { op: "update_measure", name: "Margin", expression: "DIVIDE([Profit],[Sales],0)" });
  assert.deepEqual(editing.measureUpdateOp(original, { expression: original.Expression, format: "0.00%", description: "New words" }), { op: "update_measure", name: "Margin", format_string: "0.00%", description: "New words" });
  assert.deepEqual(editing.createMeasureOp({ table: "Sales", name: " Total ", expression: " SUM(Sales[Amount]) ", format: "" }), { op: "create_measure", table: "Sales", name: "Total", expression: "SUM(Sales[Amount])" });
  assert.deepEqual(editing.createMeasureOp({ table: "Sales", name: "T", expression: "1", format: "#,0", description: "d" }), { op: "create_measure", table: "Sales", name: "T", expression: "1", format_string: "#,0", description: "d" });
  assert.equal(editing.measureNameProblem("  ", []), "Give the measure a name.");
  assert.equal(editing.measureNameProblem("margin", ["Margin"]), "A measure named “margin” already exists.");
  assert.equal(editing.measureNameProblem("Margin", ["Margin"], "Margin"), undefined);
  assert.equal(editing.measureNameProblem("x".repeat(201), []), "Measure names can have at most 200 characters.");
});

test("formatting-only batches skip the data queries and name the visuals to re-read", () => {
  assert.equal(editing.isFormattingOnly([{ op: "set_alt_text", visual: "a", text: "x" }, { op: "set_position", visual: "b", x: 1 }]), true);
  assert.equal(editing.isFormattingOnly([{ op: "set_position", visual: "a", x: 1 }, { op: "set_fields", visual: "a", role: "Y" }]), false);
  assert.equal(editing.isFormattingOnly([{ op: "delete_visual", visual: "a" }]), false);
  assert.equal(editing.isFormattingOnly([{ op: "add_page", name: "x" }]), false);
  assert.equal(editing.isFormattingOnly([]), false);
  assert.deepEqual(editing.touchedVisuals([{ op: "set_alt_text", visual: "a" }, { op: "set_position", visual: "b" }, { op: "arrange", visual: "a" }]), ["a", "b"]);
  assert.equal(editing.touchedVisuals([{ op: "set_page_size", page: "p", width: 1280, height: 720 }]), undefined);
  assert.equal(editing.touchedVisuals([{ op: "set_page_size", page: "p" }, { op: "set_position", visual: "a" }]), undefined);
});

test("an answered question becomes an applied receipt card", () => {
  const receipt = readReceipt(receiptBody("r0012", { protected: [{ visual: "v", what: "conditional font color on Agent ID" }], visible_in: [{ op: "set_conditional_format", where: "powerbi_only" }] }));
  const applied = editing.receiptToApplied(receipt);
  assert.deepEqual([applied.type, applied.revisionId, applied.parentId, applied.head.id, applied.canUndo], ["applied", "r0012", "r0007", "r0012", true]);
  const cards = addEditCard([], applied, "c1");
  assert.equal(receiptCanUndo(cards[0], "r0012"), true);
  assert.equal(describeReceipt({ summaries: applied.summaries, warnings: applied.warnings, protected: applied.protectedItems, visible_in: applied.visibleIn, outcome: "updated" }).chips.length, 2);
});

test("an optimistic rectangle stays until the server shows the visual somewhere new", () => {
  const server = { a: R(10, 10, 50, 50), b: R(100, 100, 50, 50) };
  let map = geometry.stageOverride({}, { a: R(30, 30, 50, 50) }, id => server[id]);
  assert.deepEqual(geometry.overrideRects(map), { a: R(30, 30, 50, 50) });
  // A second gesture on the same visual keeps the ORIGINAL server rectangle as its reference.
  map = geometry.stageOverride(map, { a: R(40, 40, 50, 50), b: R(110, 100, 50, 50) }, id => server[id]);
  assert.deepEqual(map.a.from, R(10, 10, 50, 50));
  assert.deepEqual(map.b.from, R(100, 100, 50, 50));
  // A render that still has both where they were: the change is in flight, nothing is dropped (same object back).
  assert.equal(geometry.settleOverride(map, server), map);
  // The receipt landed for `a` (the server moved it), `b` is still in flight.
  const settled = geometry.settleOverride(map, { ...server, a: R(40, 40, 50, 50) });
  assert.deepEqual(Object.keys(settled), ["b"]);
  // Whatever moved it (the change, an Undo, a Desktop save), the server's answer wins; unknown visuals keep their entry.
  assert.deepEqual(Object.keys(geometry.settleOverride(map, { a: R(0, 0, 50, 50), b: undefined })), ["b"]);
});

// --- the editor's components render (server-side smoke test: no browser is started) ---------------------------------------------------

const { renderToStaticMarkup } = await import("react-dom/server");
const React = await import("react");
async function loadComponent(file) {
  const compiled = await build({ entryPoints: [fileURLToPath(new URL(`../src/${file}`, import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" } });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
}
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));
// One bundle for a component and the store it reads, so a test can queue a change the component then shows.
async function loadWithStore(file) {
  const abs = fileURLToPath(new URL(`../src/${file}`, import.meta.url)).replaceAll("\\", "/");
  const store = fileURLToPath(new URL("../src/importedEditing.ts", import.meta.url)).replaceAll("\\", "/");
  const compiled = await build({ stdin: { contents: `export * from ${JSON.stringify(abs)}; export { pendingStore } from ${JSON.stringify(store)};`, resolveDir: fileURLToPath(new URL("../src", import.meta.url)), loader: "ts" }, bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" } });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
}
const noop = () => undefined;

test("the Visual pane draws conditional formatting, number format and size & position, and no styling", async () => {
  const { FormatPane } = await loadComponent("FormatPane.tsx");
  const html = render(FormatPane, { info: chart(), catalog: undefined, disabled: false, onEdit: noop });
  for (const title of ["Conditional formatting", "Number format", "Display units", "Size &amp; position", "Analytics", "Accessibility"]) assert.ok(html.includes(title), title);
  assert.match(html, /aria-label="Decimal places"[^>]*value="1"/);
  // The report's own formatting is there to draw, but nothing in the pane changes it.
  for (const styling of ["Caption", "Title size", "Colors", "By category", "Data labels", "Legend", "Axes", "Background &amp; border", "All properties", "Format painter"]) assert.ok(!html.includes(styling), styling);
  assert.match(render(FormatPane, { info: undefined, disabled: false, onEdit: noop }), /Select a visual on the page to see its conditional formatting and settings/);
  assert.match(render(FormatPane, { info: { id: "g", kind: "group", visualType: "visualGroup" }, disabled: false, onEdit: noop }), /This is a group/);
  // An older catalog that still lists styling controls: none of them shows.
  const withCatalog = render(FormatPane, { info: chart(), catalog: staleStylingCatalog(), disabled: true, onEdit: noop });
  for (const styling of ["Layout &amp; style", "Background &amp; border", "Legend", "Title text"]) assert.ok(!withCatalog.includes(styling), styling);
  assert.ok(withCatalog.includes("Size &amp; position") && withCatalog.includes('aria-busy="true"'));
  // A table: its structure, and the column picker for column names and order.
  const table = render(FormatPane, { info: { id: "t1", kind: "table", visualType: "tableEx", rect: R(0, 0, 400, 240), columns: [{ kind: "measure", table: "Orders", name: "Sales", label: "Sales", role: "Values", query_ref: "Orders.Sales" }] }, catalog: tableCatalog(), disabled: false, onEdit: noop });
  for (const title of ["Conditional formatting", "Number format", "Format code", "Columns", "Header text", "Position", "Table layout", "Totals", "Size &amp; position"]) assert.ok(table.includes(title), title);
  for (const styling of ["Banded rows", "Band color", "Wrap values", "Width", "Title text", "Display units"]) assert.ok(!table.includes(styling), styling);
});

test("page tabs, fields, selection list and pending changes render", async () => {
  const { PageTabs } = await loadComponent("PageTabs.tsx");
  const tabs = render(PageTabs, { pages: [{ id: "p1", title: "Overview", width: 1280, height: 720, visuals: [1, 2] }, { id: "p2", title: "Detail", width: 960, height: 720, hidden: true, visuals: [] }], pageId: "p1", disabled: false, onSelect: noop, onEdit: async () => true });
  assert.ok(tabs.includes("Overview") && tabs.includes("2 visuals") && tabs.includes("0 visuals · hidden"));
  assert.match(tabs, /aria-current="page"/);
  assert.match(tabs, /aria-label="Add a page"/);
  assert.ok(tabs.includes("16:9 (1280 × 720)"));
  // The page background is the report's own: the page settings do not change it.
  assert.ok(!tabs.includes("Background"));
  const { FieldsPane } = await loadComponent("FieldsPane.tsx");
  const catalog = format.readFormatCatalog({ roles: [{ role: "Category", kind: "Grouping", label: "X-axis" }, { role: "Y", kind: "Measure", label: "Y-axis" }] }, "x");
  const fields = render(FieldsPane, { info: chart(), catalog, choices: [], measures: [], disabled: false, onEdit: noop });
  assert.ok(fields.includes("X-axis") && fields.includes("Region") && fields.includes("Y-axis") && fields.includes("Sales"));
  assert.ok(fields.includes("Remove Sales from Y-axis"));
  assert.ok(render(FieldsPane, { info: undefined, choices: [], measures: [], disabled: false, onEdit: noop }).includes("Select a visual"));
  const { SelectionList } = await loadComponent("SelectionList.tsx");
  const list = render(SelectionList, { visuals: [{ id: "a", title: "Sales", kind: "column", hidden: false }, { id: "b", title: "Ghost", kind: "card", hidden: true }], selectedIds: ["a"], disabled: false, onSelect: noop, onHidden: noop, onArrange: noop });
  assert.ok(list.includes("Visuals on this page (2)") && list.includes('aria-label="Show Ghost"') && list.includes('aria-label="Hide Sales"'));
  const { PendingModelChanges, pendingStore: trayStore } = await loadWithStore("PendingModelChanges.tsx");
  trayStore.clear("smoke");
  assert.equal(render(PendingModelChanges, { workspaceId: "smoke", disabled: false, onApply: noop }), "");
  trayStore.add("smoke", { op: "create_measure", table: "Sales", name: "Margin", expression: "1+1" });
  const pending = render(PendingModelChanges, { workspaceId: "smoke", capabilities: { edit_report: true, edit_model: false, reasons: ["Power BI Desktop is not installed"], thin_report: false }, disabled: false, onApply: noop });
  assert.ok(pending.includes("Pending Power BI changes (1)") && pending.includes("Add the measure Margin to Sales") && pending.includes("1+1"));
  assert.ok(pending.includes("Apply to report (needs Power BI, about 1 min)") && pending.includes("Power BI Desktop is not installed"));
  assert.match(pending, /<button[^>]*disabled=""[^>]*>Apply to report/);
  trayStore.clear("smoke");
});

test("chat receipts, questions and progress render; Undo is off on an old receipt", async () => {
  const { EditChatCards } = await loadComponent("EditChatCards.tsx");
  const applied = id => ({ id: `c${id}`, type: "applied", event: parseChatEditEvent("edit_applied", { revision_id: `r000${id}`, head: { id: `r000${id}`, hash: "" }, summaries: [`Change ${id}.`], visible_in: [{ op: "set_conditional_format", where: "powerbi_only" }] }) });
  const cards = [applied(1), applied(2), { id: "q", type: "choices", event: parseChatEditEvent("edit_choices", { question: "Which chart?", choices: [{ index: 0, label: "Sales by Region" }, { index: 1, label: "Sales by Month" }], pending_id: "p" }) },
    { id: "m", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind: "model_edit", what_if: { question: "target 120K", answer: "41 agents" }, detail: "Target = 120000", pending_id: "p2" }) },
    { id: "f", type: "failed", event: parseChatEditEvent("edit_failed", { message: "No visual called Foo." }) }];
  const html = render(EditChatCards, { cards, progress: { stage: "opening_desktop", label: "Opening Power BI" }, headId: "r0002", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop });
  const undoButtons = [...html.matchAll(/<button[^>]*>(?:<svg[^>]*>.*?<\/svg>)?Undo<\/button>/g)].map(m => /disabled=""/.test(m[0]));
  assert.deepEqual(undoButtons, [true, false]);
  assert.ok(html.includes("Change 1.") && html.includes("Conditional formatting: visible in Power BI only"));
  assert.ok(html.includes("Which chart?") && html.includes("Sales by Month"));
  assert.ok(html.includes("Apply to report (needs Power BI, about 1 min)") && html.includes("41 agents") && html.includes("Target = 120000"));
  assert.ok(html.includes("No visual called Foo."));
  assert.ok(html.includes("Opening Power BI") && html.includes("Power BI Desktop will open briefly; don&#x27;t use it until Studio closes it."));
  assert.equal(render(EditChatCards, { cards: [], headId: "r1", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop }), "");
});

test("measure editor and history pane render", async () => {
  const { MeasureEditor } = await loadComponent("MeasureEditor.tsx");
  const measure = { TableName: "Sales", Name: "Margin", Expression: "DIVIDE([Profit],[Sales])", FormatString: "0.0%" };
  const html = render(MeasureEditor, { workspaceId: "w", measure, all: [measure], disabled: false });
  assert.ok(html.includes("DIVIDE([Profit],[Sales])") && html.includes("Queue this change") && html.includes("Rename…") && html.includes("Delete…"));
  const { HistoryPane } = await loadComponent("HistoryPane.tsx");
  const rows = readHistory([H("r3", { parent: "r2", is_head: true, can_undo: true, summary: "Made the bars green." }), H("r2", { parent: "r1", author: "desktop", summary: "Saved in Power BI", conflict: true }), H("r1", { author: "import", summary: "Imported the report", available: false })]);
  const fake = { history: rows, historyState: { loading: false }, loadHistory: async () => undefined, head: { id: "r3", hash: "" }, client: {}, undo: async () => ({ status: "cancelled" }), restore: async () => ({ status: "cancelled" }) };
  const pane = render(HistoryPane, { workspaceId: "w", editing: fake, disabled: false });
  assert.ok(pane.includes("Made the bars green.") && pane.includes("Current") && pane.includes("Power BI Desktop") && pane.includes("Not kept on disk"));
  assert.ok(pane.includes("Saved in Power BI while Studio had newer changes"));
  assert.equal((pane.match(/>Undo<\/button>/g) ?? []).length, 1);
  assert.equal((pane.match(/Restore to before this/g) ?? []).length, 1);
  // The oldest version has nothing before it and is not on disk: no restore is offered for it.
  assert.equal((pane.match(/Restore this version/g) ?? []).length, 0);
});
