import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [fileURLToPath(new URL("../src/daxConversation.ts", import.meta.url))], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { daxTurnsFromEvents, daxConversationsFromEvents, mergeDaxTurns, reconcileDaxTurns, activeDaxPending, saveDaxConversationState, daxDiscussion, daxHandoffDiscussion, daxRequestCanApply, daxSourceIdentity, isDaxClarification, daxConversationKey, readDaxConversation, archiveDaxTurns, currentDaxTurns, readDaxArchives, daxArchiveKey, daxArchiveId, daxComposerKey, readDaxComposer, daxPendingKey, readDaxPending } = loaded.exports;
const turn = (goal, response, id) => ({ goal, response, kind: "advice", id });

test("history recovery is scoped to the imported snapshot and retains interrupted evidence", () => {
  const pair = (request_id, snapshot_id, status = "completed") => [
    { type: "dax.assistant_requested", payload: { request_id, snapshot_id, goal: "Help me choose" } },
    { type: "dax.assistant_answered", payload: { request_id, snapshot_id, status, mode: "advice", response: "Which metric?" } },
  ];
  const events = [...pair("a", "s1"), ...pair("b", "s2"), ...pair("c", "s1", "cancelled"), { type: "message.created", payload: { content: "Workspace chat" } }];
  const recovered = daxTurnsFromEvents(events, "s1");
  assert.deepEqual(recovered.map(t => t.id), ["a", "c"]);
  assert.equal(recovered[1].status, "cancelled");
  assert.equal(recovered[1].kind, "advice");
  assert.equal(daxTurnsFromEvents(events, "s3").length, 0);
  assert.notEqual(daxConversationKey("w1", "s1"), daxConversationKey("w2", "s1"));
  assert.notEqual(daxConversationKey("w1", "s1"), daxConversationKey("w1", "s2"));
});

test("audit recovery deduplicates cached answers and keeps local save metadata", () => {
  const original = turn("How?", "Use a sum.", "a");
  const cached = { ...original, id: undefined, created: "Total" };
  const merged = mergeDaxTurns([original], [cached, turn("Why?", "It adds rows.", "b")]);
  assert.equal(merged.length, 2);
  assert.equal(merged[0].id, "a");
  assert.equal(merged[0].created, "Total");
  assert.equal(mergeDaxTurns(merged, merged).length, 2);
  const complete = { ...original, status: "completed" };
  const stale = { ...original, status: "interrupted", response: "Partial" };
  assert.equal(mergeDaxTurns([complete], [stale])[0].response, "Use a sum.");
  assert.equal(mergeDaxTurns([stale], [complete])[0].status, "completed");
});

test("durable receipts replace stale browser transcript fields without losing successful local save notes", () => {
  const receipt = { ...turn("Recorded question", "Authoritative response", "request"), status: "completed", createdAt: "2026-10-05T10:00:00Z" };
  const cached = { ...receipt, goal: "Stale question", response: "Stale response", createdAt: "2026-10-05T09:00:00Z", created: "Saved measure" };
  assert.deepEqual(reconcileDaxTurns([receipt], [cached]), [{ ...receipt, created: "Saved measure" }]);
  const incomplete = { ...receipt, status: "cancelled", response: "Native partial response", formula: undefined, kind: "advice" };
  const localFailure = { ...cached, status: "failed", response: "", error: "Network failed" };
  const recovered = reconcileDaxTurns([incomplete], [localFailure])[0];
  assert.equal(recovered.response, "Native partial response");
  assert.equal(recovered.status, "cancelled");
  assert.equal(recovered.created, undefined);
});

test("archived recovery envelopes cannot revive in a fresh chat, while failed active requests remain retryable", () => {
  const pending = { requestId: "old", goal: "Original request", expression: "1", mode: "auto" };
  const failed = { ...turn("Original request", "", "old"), status: "failed" };
  assert.equal(activeDaxPending(pending, [failed], []), pending);
  assert.equal(activeDaxPending(pending, [{ ...failed, status: "completed" }], []), undefined);
  assert.equal(activeDaxPending(pending, [], [{ endedAt: "2026-10-05", turns: [failed] }]), undefined);
  assert.equal(activeDaxPending(pending, [], []), pending, "a rejected request with no server receipt remains recoverable");
});

test("New chat boundaries reject old recovery even when the same formula has no archived receipt", () => {
  const pending = { requestId: "rejected-before-persist", goal: "Original request", expression: "SUM(Orders[Sales])", mode: "auto", chatBoundary: 12 };
  assert.equal(activeDaxPending(pending, [], [], 12), pending);
  assert.equal(activeDaxPending(pending, [], [], 18), undefined);
  assert.equal(activeDaxPending({ ...pending, chatBoundary: undefined }, [], [], 18), undefined, "legacy requests belong only to the initial chat");
  const initial = { ...pending, chatBoundary: undefined };
  assert.equal(activeDaxPending(initial, [], [], 0), initial);
  assert.equal(activeDaxPending({ ...pending, chatBoundary: 0 }, [], [], 0)?.requestId, pending.requestId);
});

test("browser-only failures do not return after a scoped durable clear", () => {
  const old = { ...turn("Rejected before persistence", "", "failed"), status: "failed", chatBoundary: 12 };
  const current = { ...turn("Current request", "Current answer", "new"), chatBoundary: 18 };
  assert.deepEqual(currentDaxTurns([old, current], [], 18), [current]);
  assert.equal(daxDiscussion(currentDaxTurns([old], [], 18)), undefined);
});

test("a chat-copy quota failure still saves the composer and exact retry envelope", () => {
  const values = new Map();
  const order = [];
  const storage = { setItem(key, value) { order.push(key); if (key === "chat") throw new Error("Quota"); values.set(key, value); }, removeItem(key) { values.delete(key); } };
  const pending = { requestId: "request", goal: "Original", expression: "1 + 2", error: "prior", mode: "fix", discussion: "Original context" };
  assert.deepEqual(saveDaxConversationState(storage, { storageKey: "chat", turns: [turn("Q", "A")], composerKey: "composer", composer: "Next question", pendingKey: "retry", pending }), ["chat copy"]);
  assert.equal(values.get("composer"), "Next question");
  assert.deepEqual(JSON.parse(values.get("retry")), pending);
  assert.deepEqual(order, ["composer", "retry", "chat"]);
  assert.deepEqual(saveDaxConversationState(storage, { storageKey: "chat", turns: [], pendingKey: "retry" }), ["chat copy"]);
  assert.equal(values.has("retry"), false);
});

test("one storage field failure does not skip the remaining conversation state", () => {
  const values = new Map();
  const storage = { setItem(key, value) { if (key === "composer") throw new Error("Blocked"); values.set(key, value); }, removeItem() { throw new Error("Blocked deletion"); } };
  assert.deepEqual(saveDaxConversationState(storage, { storageKey: "chat", turns: [turn("Q", "A")], composerKey: "composer", composer: "Keep me", pendingKey: "retry" }), ["typed message", "request recovery"]);
  assert.equal(JSON.parse(values.get("chat"))[0].goal, "Q");
});

test("bounded dialogue preserves recent clarification and complete user/assistant turns", () => {
  const turns = [turn("Old topic", "x".repeat(1600)), turn("Sales minus Discount?", "Money or rate?"), turn("Money, before discounts", "We can subtract the amounts.")];
  const dialogue = daxDiscussion(turns);
  assert.ok(dialogue.length <= 2000);
  assert.match(dialogue, /User: Money, before discounts\nAssistant: We can subtract/);
  assert.match(dialogue, /Sales minus Discount/);
  assert.equal(daxDiscussion([]), undefined);
  assert.ok(daxDiscussion([turn("Newest", "y".repeat(4000))]).length <= 2000);
  assert.doesNotMatch(daxDiscussion([turn("Explain", "Answer\n\nReferences supplied: long footer")]), /References supplied/);
});

test("DAX handoff carries only the labeled source question and bounded current DAX dialogue", () => {
  const turns = [turn("Earlier DAX question", "x".repeat(1500)), turn("Keep the West filter", "We can keep that filter.")];
  const context = daxHandoffDiscussion("Calculate sales for West", "Workspace chat", turns);
  assert.match(context, /^Earlier Workspace request \(Workspace chat\):\nCalculate sales for West/);
  assert.match(context, /User: Keep the West filter\nAssistant: We can keep that filter\./);
  assert.ok(context.length <= 2000);
  const long = daxHandoffDiscussion("Q".repeat(4000), "Source\nlabel", [turn("Latest refinement", "A".repeat(4000))]);
  assert.ok(long.length <= 2000);
  assert.match(long, /Source label/);
  assert.match(long, /Latest refinement/);
  assert.equal(daxHandoffDiscussion("", "Source", turns), daxDiscussion(turns));
  assert.equal(daxHandoffDiscussion("Source request", "Source", [], 0), undefined);
  assert.equal(daxDiscussion(turns, 1), undefined);
  assert.equal(daxDiscussion([]), undefined, "a detached source with a new chat supplies no earlier dialogue");
});

test("automatic editor updates require the captured formula, source request, and chat boundary", () => {
  const source = { request: "Calculate sales for West", sourceLabel: "Workspace chat", sourceMessageId: 4, code: "West Sales = SUM(Orders[Sales])" };
  const input = { requestId: "candidate", mode: "generate", goal: "Refine this", expression: "SUM(Orders[Sales])", chatBoundary: 4, discussion: daxHandoffDiscussion(source.request, source.sourceLabel, [turn("Keep returns separate", "Yes")]), sourceIdentity: daxSourceIdentity(source) };
  assert.equal(daxRequestCanApply(input, input.expression, 4, source), true);
  assert.equal(daxRequestCanApply(input, "SUM(Orders[Profit])", 4, source), false);
  assert.equal(daxRequestCanApply(input, input.expression, 8, source), false);
  assert.equal(daxRequestCanApply(input, input.expression, 4), false, "detaching the source prevents its old answer from replacing the scratchpad");
  assert.equal(daxRequestCanApply(input, input.expression, 4, { ...source, request: "Calculate sales for East" }), false);
  assert.equal(daxRequestCanApply(input, input.expression, 4, { ...source, sourceLabel: "Different source chat" }), false);
  assert.equal(daxRequestCanApply(input, input.expression, 4, { ...source, sourceMessageId: 9 }), false);
  assert.equal(daxRequestCanApply(input, input.expression, 4, { ...source, code: "West Sales = SUM(Orders[Sales]) + 1" }), false);
  assert.equal(daxRequestCanApply({ ...input, sourceIdentity: undefined }, input.expression, 4, source), false, "legacy handoff recovery cannot prove original code or message identity");
  const plain = { ...input, chatBoundary: 0, discussion: daxDiscussion([turn("Current DAX question", "Answer")]), sourceIdentity: daxSourceIdentity() };
  assert.equal(daxRequestCanApply(plain, plain.expression, 0), true);
  assert.equal(daxRequestCanApply(plain, plain.expression, 0, source), false, "attaching a source while responding also requires explicit use of the older candidate");
});

test("guarded clarification is distinct from an explanation, formula, and interrupted evidence", () => {
  const clarification = turn("Build a calculation", "This pattern does not represent all the selected calculation options. No formula or report change was applied.");
  assert.equal(isDaxClarification(clarification), true);
  assert.equal(isDaxClarification({ ...clarification, status: "completed" }), true);
  assert.equal(isDaxClarification({ ...clarification, status: "cancelled" }), false);
  assert.equal(isDaxClarification({ ...clarification, kind: "formula" }), false);
  assert.equal(isDaxClarification(turn("Explain SUM", "SUM adds values within filter context.")), false);
});

test("a new chat archives the thread and replayed evidence stays out of it", () => {
  const first = turn("Return rate?", "Which field marks returns?", "a");
  const second = { ...turn("Use Returns[Returned]", "DIVIDE(...)", "b"), kind: "formula" };
  const archives = archiveDaxTurns([], [first, second], "2026-09-24T01:00:00Z");
  assert.equal(archives.length, 1);
  assert.equal(archiveDaxTurns(archives, []).length, 1, "an empty chat is not archived");
  // Engine events replay every past turn; only the new one belongs to the current chat.
  const fresh = turn("Profit margin?", "SUM/SUM", "c");
  assert.deepEqual(currentDaxTurns([first, second, fresh], archives).map(t => t.id), ["c"]);
  // Turns cached without an id are matched by their text.
  assert.deepEqual(currentDaxTurns([{ ...first, id: undefined }], archives), []);
  // A new chat sends no earlier dialogue to the model.
  assert.equal(daxDiscussion(currentDaxTurns([first, second], archives)), undefined);
  let many = [];
  for (let index = 0; index < 25; index += 1) many = archiveDaxTurns(many, [turn(`Q${index}`, "A", `id${index}`)]);
  assert.equal(many.length, 20);
  assert.equal(many[0].turns[0].goal, "Q5");
  assert.notEqual(daxArchiveKey("w1", "s1"), daxConversationKey("w1", "s1"));
  assert.deepEqual(currentDaxTurns([{ ...first, id: "new-receipt" }], archives).map(t => t.id), ["new-receipt"], "a new chat can repeat the same question without losing its answer");
});

const evidence = (sequence, type, payload) => ({ sequence, type, payload, created_at: `2026-10-04T${String(sequence % 24).padStart(2, "0")}:00:00Z` });
const requested = (sequence, request_id, snapshot_id = "current") => evidence(sequence, "dax.assistant_requested", { request_id, snapshot_id, goal: `Question ${request_id}` });
const answered = (sequence, request_id, snapshot_id = "current", status = "completed") => evidence(sequence, "dax.assistant_answered", { request_id, snapshot_id, status, mode: "advice", response: `Answer ${request_id}` });
const cleared = (sequence, through_sequence, snapshot_id = "current") => evidence(sequence, "dax.chat.cleared", { through_sequence, snapshot_id });

test("durable clear boundaries recover independent chats, even when answer delivery is late", () => {
  const events = [requested(1, "old"), answered(2, "old"), requested(3, "late"), cleared(4, 3), answered(5, "late"), requested(6, "next"), answered(7, "next"), cleared(8, 7), requested(9, "active"), answered(10, "active")];
  // Event pagination can arrive out of order; sequence defines the durable chronology.
  const recovered = daxConversationsFromEvents([...events].reverse(), "current");
  assert.deepEqual(recovered.current.map(t => t.id), ["active"]);
  assert.deepEqual(recovered.archives.map(a => a.turns.map(t => t.id)), [["old", "late"], ["next"]]);
  assert.deepEqual(recovered.archives.map(daxArchiveId), ["dax-chat-4", "dax-chat-8"]);
  assert.equal(recovered.hasBoundary, true);
  assert.equal(recovered.boundaryId, 7);
  assert.doesNotMatch(daxDiscussion(recovered.current), /Question old|Question next|Question late/);
});

test("previous data remains readable without entering the current model context", () => {
  const events = [requested(1, "previous-closed", "previous"), answered(2, "previous-closed", "previous"), cleared(3, 2, "previous"), requested(4, "previous-open", "previous"), answered(5, "previous-open", "previous"), requested(6, "current"), answered(7, "current"), requested(8, "incomplete"), answered(9, "incomplete", "current", "cancelled")];
  const recovered = daxConversationsFromEvents(events, "current");
  assert.deepEqual(recovered.current.map(t => t.id), ["current", "incomplete"]);
  assert.doesNotMatch(daxDiscussion(recovered.current), /Question incomplete/);
  assert.deepEqual(recovered.archives.map(a => a.turns[0].id), ["previous-closed", "previous-open"]);
  assert.ok(recovered.archives.every(a => a.previousData && a.snapshotId === "previous"));
  assert.equal(recovered.hasBoundary, false);
  assert.equal(recovered.boundaryId, 0, "a previous-data clear cannot replace the current boundary");
  assert.equal(daxConversationsFromEvents(events, "previous").boundaryId, 2);
  assert.equal(daxConversationsFromEvents([requested(1, "bad", "previous"), answered(2, "bad", "current")], "current").current.length, 0);
});

test("all durable chats survive the old browser archive limit and repeated event receipts", () => {
  const events = [];
  for (let index = 0; index < 25; index += 1) events.push(requested(index * 3 + 1, `q${index}`), answered(index * 3 + 2, `q${index}`), cleared(index * 3 + 3, index * 3 + 2));
  events.push(requested(76, "active"), answered(77, "active"), answered(78, "active"));
  const recovered = daxConversationsFromEvents(events, "current");
  assert.equal(recovered.archives.length, 25);
  assert.equal(recovered.archives[0].turns[0].id, "q0");
  assert.deepEqual(recovered.current.map(t => t.id), ["active"]);
  assert.equal(new Set(recovered.archives.map(daxArchiveId)).size, 25);
});

test("incomplete DAX never becomes an applicable formula or model dialogue", () => {
  for (const status of ["interrupted", "cancelled", "failed"]) {
    const events = [requested(1, "partial"), evidence(2, "dax.assistant_answered", { request_id: "partial", snapshot_id: "current", status, mode: "generate", response: "```dax\nTotal = SUM('Orders'[Sales])\n```", calculation_request: "Create a sum", error: "Generation ended" }), cleared(3, 2)];
    const recovered = daxConversationsFromEvents(events, "current");
    const partial = recovered.archives[0].turns[0];
    assert.equal(partial.status, status);
    assert.equal(partial.kind, "advice");
    assert.equal(partial.formula, undefined);
    assert.equal(partial.calculationRequest, undefined);
    assert.equal(partial.error, "Generation ended");
    assert.equal(daxDiscussion([partial]), undefined);
    assert.equal(recovered.current.length, 0);
  }
});

test("typed messages and original retry envelopes recover only within their workspace and data", () => {
  const stored = new Map();
  globalThis.localStorage = { getItem: key => stored.get(key) ?? null };
  try {
    const composer = daxComposerKey("w1", "s1");
    stored.set(composer, "  My typed follow-up  ");
    assert.equal(readDaxComposer(composer), "  My typed follow-up  ");
    assert.equal(readDaxComposer(daxComposerKey("w2", "s1")), "");
    assert.equal(readDaxComposer(daxComposerKey("w1", "s2")), "");
    const pending = { requestId: "stable-request", goal: "Explain", expression: "1 + 2", error: "prior error", mode: "fix", discussion: "prior dialogue", chatBoundary: 14, sourceIdentity: daxSourceIdentity({ request: "Explain sales", sourceLabel: "Source chat", sourceMessageId: 7, code: "Total = SUM(Orders[Sales])" }) };
    const key = daxPendingKey("w1", "s1");
    stored.set(key, JSON.stringify(pending));
    assert.deepEqual(readDaxPending(key), pending);
    const escapedSource = { ...pending, sourceIdentity: daxSourceIdentity({ request: "\\".repeat(4000), sourceLabel: "Source chat", sourceMessageId: 7, code: "\n".repeat(4000) }) };
    stored.set(key, JSON.stringify(escapedSource));
    assert.deepEqual(readDaxPending(key), escapedSource, "JSON escaping must not reject a source within the original text bounds");
    assert.equal(readDaxPending(daxPendingKey("w2", "s1")), undefined);
    assert.equal(readDaxPending(daxPendingKey("w1", "s2")), undefined);
    for (const invalid of [{ ...pending, requestId: undefined }, { ...pending, mode: "invalid" }, { ...pending, expression: "x".repeat(4001) }, { ...pending, discussion: 1 }, { ...pending, goal: null }, { ...pending, chatBoundary: -1 }, { ...pending, chatBoundary: 1.5 }, { ...pending, chatBoundary: "14" }, { ...pending, chatBoundary: Number.MAX_SAFE_INTEGER + 1 }, { ...pending, sourceIdentity: "bad" }, { ...pending, sourceIdentity: "[]" }, { ...pending, sourceIdentity: JSON.stringify(["Question", "Source", {}, "1"]) }]) {
      stored.set(key, JSON.stringify(invalid)); assert.equal(readDaxPending(key), undefined);
    }
    stored.set(key, "{broken"); assert.equal(readDaxPending(key), undefined);
  } finally { delete globalThis.localStorage; }
});

test("malformed browser history cannot break DAX Lab", () => {
  globalThis.localStorage = { getItem: () => '{broken' };
  assert.deepEqual(readDaxConversation("key"), []);
  globalThis.localStorage.getItem = () => JSON.stringify([null, {}, turn("Question", "Answer"), { goal: "Bad", response: "Bad", kind: "formula", formula: {} }, { ...turn("Invalid save metadata", "A"), created: {} }]);
  assert.equal(readDaxConversation("key").length, 1);
  globalThis.localStorage.getItem = () => JSON.stringify([null, { endedAt: 1 }, { id: {}, endedAt: "2026-09-24", turns: [turn("Bad id", "A")] }, { snapshotId: {}, endedAt: "2026-09-24", turns: [turn("Bad scope", "A")] }, { previousData: "yes", endedAt: "2026-09-24", turns: [turn("Bad metadata", "A")] }, { endedAt: "2026-09-24", turns: [null, turn("Q", "A")] }]);
  assert.deepEqual(readDaxArchives("key").map(a => a.turns.length), [1]);
  globalThis.localStorage.getItem = () => "{broken";
  assert.deepEqual(readDaxArchives("key"), []);
  delete globalThis.localStorage;
});
