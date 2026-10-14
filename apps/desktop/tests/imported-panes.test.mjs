// Round 3 panes of the imported-report editor (workstream ui_panes): the pure rules of the panes, the judgement features
// (review, report tests, explain), the planner's plan card, and smoke renders of the new components. No browser is started.
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
const judgement = await load("judgement.ts");
const panes = await load("importedPanes.ts");
const extras = await load("formatExtras.ts");
const editing = await load("importedEditing.ts");
const format = await load("importedFormat.ts");
const { renderToStaticMarkup } = await import("react-dom/server");
const React = await import("react");
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));
const noop = () => undefined;

// --- fixtures ---------------------------------------------------------------------------------------------------------------

const finding = (extra = {}) => ({
  id: "near_miss-3f2a9c01be", rule: "near_miss", severity: "suggestion", visuals: ["b404", "97d1"], page: "ReportSection1",
  message: "The 'Total MTD Sales' stacked column chart is 1.4 px out of line with the chart below it.",
  fix: [{ op: "set_position", visual: "b404", x: 172.97 }], fix_summary: "Move it 1.4 px so their left edges line up.", ...extra,
});
const table = () => ({
  id: "t1", kind: "table", visualType: "tableEx", title: "Agents",
  columns: [
    { kind: "column", table: "DimEmployee", name: "EmployeeKey", label: "Agent", role: "Values", query_ref: "DimEmployee.EmployeeKey" },
    { kind: "measure", table: "# Measures", name: "MTD Sales", label: "MTD Sales", role: "Values", query_ref: "# Measures.MTD Sales", backgroundIndex: 3 },
    { kind: "measure", table: "# Measures", name: "Rank", label: "Rank", role: "Values", query_ref: "# Measures.Rank", colorIndex: 4, dataBars: { positive: "#5B9BD5" } },
    { kind: "computed", name: "values.backColor", label: "values.backColor", role: "Formatting", hidden: true, computed: "gradient" },
    { kind: "measure", name: "CF", label: "CF", role: "Formatting", hidden: true, query_ref: "# Measures.CF" },
  ],
  rows: [[201, 1000.5, 1, "#ffffff", "white"], [247, 2000, 2, "#000000", "black"], [201, 1000.5, 1, "#ffffff", "white"]],
  formatted_rows: [[null, "$1,000.50", null, null, null], [null, "$2,000.00", null, null, null], [null, "$1,000.50", null, null, null]],
  formatting: { table: { columns: { "# Measures.MTD Sales": { width: 150, alignment: "right" } } } },
});
const chart = () => ({
  id: "c1", kind: "column", visualType: "clusteredColumnChart", title: "Sales by Region",
  columns: [
    { kind: "column", table: "Orders", name: "Region", label: "Region", role: "Category", query_ref: "Orders.Region" },
    { kind: "measure", table: "Orders", name: "Sales", label: "Sales", role: "Y", query_ref: "Orders.Sales" },
    { kind: "measure", table: "Orders", name: "Profit", label: "Profit", role: "Y", query_ref: "Orders.Profit" },
  ],
  rows: [["West", 5, 1], ["East", 3, 2]], formatted_rows: [[null, "5", "1"], [null, "3", "2"]],
  formatting: { series: { ruleColors: [{ index: 3 }] } },
  referenceLines: [
    { id: "0", object: "y1AxisReferenceLine", kind: "constant", axis: "value", value: 4, name: "Target", color: "#FF0000", style: "dotted" },
    { id: "1", object: "referenceLine", kind: "average", value: 4, color: "#00FF00" },
    { id: "2", object: "referenceLine", kind: "average", value: 1.5 },
  ],
});
const bookmarks = () => [
  { name: "B1", displayName: "Overview", page: "p1", targets: null, captures: { data: true, display: true, page: true }, hidden: ["v2"], shown: ["v1"] },
  { name: "B2", displayName: "Top agents", group: { name: "G1", displayName: "Views" }, page: "p1", targets: ["v1", "v3"], captures: { data: false, display: true, page: true }, hidden: ["v3"], shown: ["v1"] },
  { name: "B3", displayName: "Detail", group: { name: "G1", displayName: "Views" }, page: "p2", targets: null, captures: { data: false, display: false, page: true }, hidden: [], shown: [] },
  { name: "B4", displayName: "Bookmark 5", page: "p1", targets: null, captures: { data: false, display: true, page: false }, hidden: [], shown: [] },
];
const renderBody = () => ({
  page: { id: "p1", title: "Overview", page_type: "drillthrough", binding: { type: "drillthrough", fields: [{ field: "DimStore.StoreName", table: "DimStore", column: "StoreName" }], keepAllFilters: false, crossReport: true } },
  bookmarks: [
    { name: "B1", displayName: "Overview", group: null, page: "p1", targets: null, captures: { data: true, display: true, page: true }, hidden: [], shown: [] },
    { name: "B2", displayName: "Grouped", group: { name: "G", displayName: "Views" }, page: "p1", targets: ["v1"], captures: { data: false }, hidden: ["v1"], shown: [] },
    { displayName: "no name" },
  ],
  interactions: { s1: { c1: "filter", c2: "highlight", c3: "none", c4: "unsupported", c5: "weird" } },
  sync_groups: { Stores: [{ page: "p1", visual: "s1", hidden: false }, { page: "p2", visual: "s9", hidden: true }, { page: "p3" }] },
  mobile: { visuals: { c1: { x: 0, y: 0, width: 320, height: 200, z: 1000 }, bad: { x: 1 } } },
  drillthroughs: [{ page: "p9", title: "Store details", fields: [{ field: "DimStore.StoreName" }], keepAllFilters: true, crossReport: false }],
  notes: ["The filters of the page this was opened from are not applied in Studio."],
  visuals: {
    g1: { title: "KPI cards", kind: "group", rank: 5, hidden: false, hidden_own: false, parent_group: null, group_children: ["k1", "k2"] },
    k1: { title: "Card 1", kind: "card", rank: 3, parent_group: "g1", group_children: [] },
    k2: { title: "Card 2", kind: "card", rank: 4, parent_group: "g1", group_children: [], hidden: true, hidden_own: true },
    s1: { title: "Store", kind: "slicer", visual_type: "slicer", rank: 2, syncGroup: { groupName: "Stores", fieldChanges: true, filterChanges: true } },
    b1: { title: "Go", kind: "button", visual_type: "actionButton", rank: 1, action: { type: "bookmark", target: "B1", valid: true, tooltip: "Open" }, position: { x: 1, y: 2, width: 30, height: 20 } },
    c1: { title: "Chart", kind: "column", rank: 6, tooltipPage: { page: "p5", title: "Tip" } },
  },
});

// --- review ------------------------------------------------------------------------------------------------------------------

test("review findings are read with their fixes; a fix that is not operations is no fix", () => {
  const found = judgement.readFindings([finding(), finding({ id: "x", severity: "problem", fix: null, message: "The 'Country' slicer filters Channel." }), { message: "" }, "junk", finding({ id: "y", fix: [{ nope: 1 }] })]);
  assert.equal(found.length, 3);
  assert.deepEqual(found[0].fix, [{ op: "set_position", visual: "b404", x: 172.97 }]);
  assert.equal(found[0].fixSummary, "Move it 1.4 px so their left edges line up.");
  assert.equal(found[1].severity, "problem");
  assert.equal(found[1].fix, undefined);
  assert.equal(found[2].fix, undefined);
  const review = judgement.readReview({ page: "ReportSection1", page_name: "1", findings: [finding(), finding({ id: "p", severity: "problem" })], skipped: { empty_visual: "Needs the page's data." }, notes: ["Text over the picture was not checked."] });
  assert.equal(review.problems, 1);
  assert.equal(review.suggestions, 1);
  assert.deepEqual(review.skipped, [{ rule: "empty_visual", reason: "Needs the page's data." }]);
  assert.equal(judgement.reviewHeadline(review), "Studio noticed 1 problem and 1 suggestion");
  assert.equal(judgement.reviewHeadline({ problems: 0, suggestions: 0 }), "Nothing to fix on this page");
  assert.equal(judgement.reviewHeadline({ problems: 2, suggestions: 0 }), "Studio noticed 2 problems");
});

test("the Review panel reviews again for the new head after its own Fix, and a moved head is not an error", async () => {
  const { reviewStarted, reviewDone, reviewFailed, reviewNeedsRerun, isHeadMoved, readReview } = judgement;
  const first = readReview({ findings: [finding()] });
  // The live sequence: review on r7, Fix makes r8, the stale request (r7) is refused with 409 head_moved.
  let state = reviewDone(reviewStarted(undefined, "r7"), first, "r7");
  assert.equal(reviewNeedsRerun(state, "r7"), false);
  assert.equal(reviewNeedsRerun(state, "r8"), true, "an edit (its own Fix, or any other) makes the review stale");
  state = reviewStarted({ ...state, fixed: ["near_miss-3f2a9c01be"] }, "r7");
  const moved = Object.assign(new Error("The report changed while this view was loading. Studio is showing the latest version."), { status: 409, code: "head_moved" });
  assert.equal(isHeadMoved(moved), true);
  assert.equal(isHeadMoved(new Error("Boom")), false);
  // The editor already knows r8: ask again at once for it.
  const known = reviewFailed(state, moved, "r7", "r8");
  assert.equal(known.retry, true);
  assert.equal(known.state.error, undefined);
  assert.equal(known.state.loading, true);
  assert.equal(known.state.review, first, "the list stays while the new review runs");
  // The editor does not know the new head yet: wait (no request loop), then re-run when r8 arrives.
  const waiting = reviewFailed(state, moved, "r7", "r7");
  assert.equal(waiting.retry, false);
  assert.equal(waiting.state.error, undefined);
  assert.equal(reviewNeedsRerun(waiting.state, "r7"), false);
  assert.equal(reviewNeedsRerun(waiting.state, "r8"), true);
  const second = readReview({ findings: [] });
  state = reviewDone(reviewStarted(waiting.state, "r8"), second, "r8");
  assert.deepEqual([state.loading, state.error, state.review.findings.length, state.head, state.fixed], [false, undefined, 0, "r8", ["near_miss-3f2a9c01be"]]);
  assert.equal(reviewNeedsRerun(state, "r8"), false);
  assert.equal(reviewNeedsRerun(undefined, "r9"), false, "a closed panel reviews nothing");
  // Other failures are shown.
  const failed = reviewFailed(state, new Error("The page could not be read."), "r8", "r8");
  assert.deepEqual([failed.retry, failed.state.loading, failed.state.error], [false, false, "The page could not be read."]);
  // The client keeps the server's code on the error.
  const client = judgement.createJudgementClient(async () => ({ status: 409, body: { code: "head_moved", message: "The report changed while this view was loading.", expected: "r7", actual: "r8" } }));
  await assert.rejects(client.review("w", "p1", "r7"), error => isHeadMoved(error) && error.code === "head_moved");
});

test("the edit receipt keeps its review, test outcome and new model health findings", () => {
  const receipt = editing.readReceipt({
    revision: { id: "r0008", parent: "r0007" }, head: { id: "r0008", hash: "h" }, summaries: ["Moved it."], warnings: ["Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80."],
    protected: [], visible_in: [], outcome: "updated", replaced: ["TopN blue"], review: [finding()],
    tests: { results: [{ id: "t1", name: "A", status: "passed", message: "A is still 1." }], broken: [], text: "1 report test still passes.", text_broken: "Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80." },
    model_health: { new: [{ id: "h1", severity: "suggestion", category: "formulas", message: "The measure Margin divides with / instead of DIVIDE.", why: "A zero gives an error." }] },
  });
  assert.deepEqual(receipt.review.map(f => f.id), ["near_miss-3f2a9c01be"]);
  assert.deepEqual(receipt.replaced, ["TopN blue"]);
  assert.deepEqual(receipt.testLines, ["1 report test still passes.", "Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80."]);
  assert.equal(receipt.modelHealth[0].message, "The measure Margin divides with / instead of DIVIDE.");
  const view = editing.describeReceipt(receipt);
  // The broken test is a warning already: it is shown once.
  assert.deepEqual(view.tests, ["1 report test still passes."]);
  assert.deepEqual(view.health, ["The measure Margin divides with / instead of DIVIDE."]);
  // Receipts without them stay as they were.
  const plain = editing.readReceipt({ revision: { id: "r1" }, head: { id: "r1" }, summaries: [], warnings: [], protected: [], visible_in: [] });
  assert.equal(plain.review, undefined);
  assert.deepEqual(editing.describeReceipt(plain).tests, []);
  assert.deepEqual(judgement.receiptTestLines({ results: [{ id: "a", status: "passed" }, { id: "b", status: "passed" }] }), ["2 of 2 report tests still pass."]);
  assert.deepEqual(judgement.receiptTestLines({ broken: [{ id: "a", status: "failed", message: "A moved." }] }), ["A moved."]);
});

// --- report tests ----------------------------------------------------------------------------------------------------------

const testsBody = () => ({
  tests: [
    { id: "t8d4", name: "Agents Avg MTD Sales", measure: { table: "# Measures", name: "Employees Avg MTD Sales" }, filters: [{ table: "Top-Bottom-N", column: "Top-Bottom-N", op: "in", values: [6] }], expect: { equals: 73514.79, tolerance_abs: null }, origin: { page: "p1", visual: "k1" }, source: "pinned" },
    { id: "t2", name: "Range", expression: "1+1", filters: [{ table: "Date", column: "Year", op: "between", values: [2019, null] }], expect: { between: [0, 100] }, origin: { visual: "k1" } },
    { id: "t3", name: "Not blank", measure: { table: "M", name: "X" }, filters: [], expect: { not_blank: true }, origin: { visual: "c1" } },
    { id: "t4", name: "No origin", measure: { table: "M", name: "Y" }, filters: [], expect: { equals: null } },
    { name: "no id" },
  ],
  last_run: { revision: "r7", engine: "Studio's DAX engine", results: [{ id: "t8d4", name: "A", status: "passed", message: "still" }, { id: "t2", name: "R", status: "failed", message: "R is 150 now instead of between 0 and 100." }, { id: "t3", status: "weird" }], text: "1 passes, 1 fails." },
  stale: true,
});

test("the tests route is read, and each visual gets the worst status of its tests", () => {
  const state = judgement.readTestsState(testsBody());
  assert.equal(state.tests.length, 4);
  assert.equal(state.stale, true);
  assert.equal(state.lastRun.results.length, 3);
  assert.equal(state.lastRun.results[2].status, "not_checked");
  const badges = judgement.visualTestBadges(state);
  assert.equal(badges.get("k1"), "failed");
  assert.equal(badges.get("c1"), "not_checked");
  assert.equal(badges.has("undefined"), false);
  assert.equal(judgement.expectText(state.tests[0].expect), "equals 73,514.79");
  assert.equal(judgement.expectText({ equals: 850, tolerance_abs: 1 }), "equals 850 (within 1)");
  assert.equal(judgement.expectText({ equals: 0.5, tolerance_rel: 0.01 }), "equals 0.5 (within 1%)");
  assert.equal(judgement.expectText(state.tests[1].expect), "is between 0 and 100");
  assert.equal(judgement.expectText({ between: [5, null] }), "is at least 5");
  assert.equal(judgement.expectText(state.tests[2].expect), "is not blank");
  assert.equal(judgement.expectText(state.tests[3].expect), "is blank");
  assert.equal(judgement.filtersText(state.tests[0].filters), "Top-Bottom-N[Top-Bottom-N] is 6");
  assert.equal(judgement.filtersText(state.tests[1].filters), "Date[Year] from 2019 to the end");
  assert.equal(judgement.filtersText([]), "No filters");
});

test("a test the user writes becomes the add route's body, or says what is missing", () => {
  const base = { name: "Sales 2009", kind: "measure", measure: { table: "Sales", name: "Total Sales" }, expectKind: "equals", equals: "1,250.5", tolerance: "0.5", low: "", high: "", filters: [{ table: "Date", column: "Year", values: "2009; 2010" }, { table: "", column: "", values: "" }] };
  assert.deepEqual(judgement.userTestBody(base), { ok: true, body: { name: "Sales 2009", measure: { table: "Sales", name: "Total Sales" }, expect: { equals: 1250.5, tolerance_abs: 0.5 }, filters: [{ table: "Date", column: "Year", op: "in", values: [2009, 2010] }] } });
  assert.deepEqual(judgement.userTestBody({ ...base, expectKind: "between", low: "10", high: "" }).body.expect, { between: [10, null] });
  assert.deepEqual(judgement.userTestBody({ ...base, expectKind: "not_blank" }).body.expect, { not_blank: true });
  assert.deepEqual(judgement.userTestBody({ ...base, equals: "West", tolerance: "" }).body.expect, { equals: "West" });
  assert.deepEqual(judgement.userTestBody({ ...base, kind: "expression", expression: " 1 + 1 " }).body.expression, "1 + 1");
  const problems = [
    [{ ...base, name: " " }, "Give the test a name."],
    [{ ...base, measure: undefined }, "Pick the measure to test."],
    [{ ...base, kind: "expression", expression: "" }, "Type the DAX expression to test."],
    [{ ...base, kind: "expression", expression: "EVALUATE ROW(\"a\", 1)" }, "Type an expression, not a query: leave out EVALUATE."],
    [{ ...base, expectKind: "between", low: "5", high: "1" }, "The lowest value is above the highest."],
    [{ ...base, expectKind: "between", low: "", high: "" }, "Give at least one end of the range."],
    [{ ...base, tolerance: "-1" }, "The tolerance must be a positive number."],
    [{ ...base, filters: [{ table: "Date", column: "Year", values: " ; " }] }, "Give the values of Date[Year], separated by semicolons."],
  ];
  for (const [draft, message] of problems) assert.deepEqual(judgement.userTestBody(draft), { ok: false, message });
  // A suggestion goes to the closed add route without its id, origin or dates.
  assert.deepEqual(judgement.suggestionBody({ id: "x", name: "N", measure: { table: "T", name: "M" }, filters: [], expect: { equals: 1 }, origin: { visual: "v" }, created_at: "d", source: "pinned", format: null }),
    { name: "N", measure: { table: "T", name: "M" }, filters: [], expect: { equals: 1 } });
  assert.equal(judgement.suggestionBody({ name: "no expectation" }), undefined);
  const suggestions = judgement.readSuggestions({ tests: [{ id: "a", name: "A", measure: { table: "T", name: "M" }, filters: [], expect: { equals: 1 } }, { junk: true }], text: ["A is 1."] });
  assert.equal(suggestions.tests.length, 1);
  assert.equal(suggestions.tests[0].text, "A is 1.");
});

test("a data point of a chart or table is picked by its category values, and pinned with the value it shows", () => {
  const visual = table();
  const points = judgement.pointChoices(visual);
  assert.deepEqual(points.map(p => p.label), ["201", "247"]);
  assert.deepEqual(points[1].values, { "DimEmployee.EmployeeKey": 247 });
  const measures = judgement.measureChoices(visual);
  assert.deepEqual(measures.map(m => [m.ref, m.index]), [["# Measures.MTD Sales", 1], ["# Measures.Rank", 2]]);
  assert.deepEqual(judgement.pointValue(visual, points[1], measures[0]), { value: 2000, shown: "$2,000.00" });
  assert.deepEqual(judgement.pinBody("p1", visual, points[1], measures[0], "  Agent 247 sales "), { page_id: "p1", visual_id: "t1", point: { "DimEmployee.EmployeeKey": 247 }, value: 2000, measure: "# Measures.MTD Sales", name: "Agent 247 sales" });
  // A card has no point and one number.
  const card = { id: "k", kind: "card", columns: [{ kind: "measure", name: "Avg", label: "Avg", query_ref: "M.Avg" }], rows: [[73514.8]], formatted_rows: [["$73,514.80"]] };
  assert.deepEqual(judgement.pointChoices(card), []);
  assert.deepEqual(judgement.pinBody("p1", card, undefined, undefined, ""), { page_id: "p1", visual_id: "k", point: {}, value: 73514.8 });
  assert.equal(judgement.pinBody("p1", { id: "s", kind: "slicer", columns: [{ kind: "column", name: "A", label: "A" }], rows: [["x"]] }, undefined, undefined, ""), undefined);
  // A right-clicked point carries every column; its categories find the choice.
  assert.equal(judgement.matchPoint(points, { "DimEmployee.EmployeeKey": 247, "# Measures.MTD Sales": 2000 }), points[1]);
  assert.equal(judgement.matchPoint(points, { other: 1 }), undefined);
  assert.equal(judgement.matchPoint(points, undefined), undefined);
});

test("the judgement client calls the documented routes and turns errors into plain messages", async () => {
  const calls = [];
  const answers = [
    { status: 200, body: { findings: [finding()] } }, { status: 200, body: testsBody() }, { status: 200, body: { id: "n", name: "N", filters: [], expect: { not_blank: true } } },
    { status: 200, body: { test: { id: "p", name: "P", filters: [], expect: { equals: 1 } }, result: { id: "p", status: "failed", message: "Studio calculates 850 here." } } },
    { status: 200, body: { results: [], text: "No tests." } }, { status: 200, body: { tests: [], text: [] } }, { status: 200, body: { id: "t2", name: "Renamed", filters: [], expect: { equals: 2 } } },
    { status: 200, body: { removed: true } }, { status: 200, body: { value: 0.4676, formatted: "46.76%", steps: [], filters: [], relationships: [], notes: [] } },
    { status: 422, body: { code: "pin_refused", message: "This visual shows a slicer, not a number.", detail: "This visual shows a slicer, not a number." } },
    { status: 422, body: { detail: [{ loc: ["body", "x"], msg: "extra" }] } },
  ];
  const client = judgement.createJudgementClient(async (method, path, body) => { calls.push([method, path, body]); return answers.shift(); });
  assert.equal((await client.review("w 1", "Report Section", "r0007")).findings.length, 1);
  assert.equal((await client.tests("w")).tests.length, 4);
  assert.equal((await client.addTest("w", { name: "N" })).id, "n");
  assert.equal((await client.pin("w", { page_id: "p" })).result.status, "failed");
  assert.equal((await client.run("w")).text, "No tests.");
  assert.deepEqual((await client.suggest("w", ["p1", "p 2"])).tests, []);
  assert.equal((await client.updateTest("w", "t/2", { name: "Renamed" })).name, "Renamed");
  assert.equal(await client.deleteTest("w", "t2"), true);
  assert.equal((await client.explain("w", { page_id: "p", visual_id: "v" })).formatted, "46.76%");
  await assert.rejects(client.pin("w", {}), /This visual shows a slicer, not a number\./);
  await assert.rejects(client.addTest("w", {}), /The request was not accepted\./);
  assert.deepEqual(calls.map(([method, path]) => `${method} ${path}`), [
    "POST /v1/workspaces/w%201/imported-report/pages/Report%20Section/review?revision=r0007", "GET /v1/workspaces/w/imported-report/tests",
    "POST /v1/workspaces/w/imported-report/tests", "POST /v1/workspaces/w/imported-report/tests/pin", "POST /v1/workspaces/w/imported-report/tests/run",
    "GET /v1/workspaces/w/imported-report/tests/suggestions?page=p1&page=p%202", "PATCH /v1/workspaces/w/imported-report/tests/t%2F2",
    "DELETE /v1/workspaces/w/imported-report/tests/t2", "POST /v1/workspaces/w/imported-report/explain", "POST /v1/workspaces/w/imported-report/tests/pin",
    "POST /v1/workspaces/w/imported-report/tests",
  ]);
  assert.deepEqual(calls[0][2], { with_data: true });
  assert.deepEqual(calls[4][2], { engine: "auto" });
});

// --- explain --------------------------------------------------------------------------------------------------------------

const explanationBody = () => ({
  page_id: "p1", visual_id: "k1", visual: "Agents with Positive Change %", kind: "card", point: { Agent: 201 }, measure: "Employees with Positive Change %",
  formula: "VAR _A = ...\nRETURN DIVIDE(_A, _B)", meaning: "The share of agents whose sales grew.", value: 0.4676, formatted: "46.76%", computed: true,
  period: "for December 2009, month to date", blank: null,
  steps: [
    { name: "_A", kind: "variable", value: 137, formatted: "137", computed: true, depth: 0 },
    { name: "[Rank MTD Desc]", kind: "measure", usage: "per_row", computed: true, depth: 1, meaning: "for every DimEmployee[EmployeeKey] (inside FILTER)" },
    { name: "_B", kind: "part", computed: false, reason: "Power BI only", depth: 9 },
  ],
  filters: [{ source: "slicer", field: "DimChannel[ChannelName]", condition: "is Store", reaches: false, on_measure: false }, { source: "page", field: "Date[Year]", reaches: true, how: "Date → FactSales" }],
  relationships: [{ tables: ["FactSales", "DimStore", "DimEmployee"], joins: ["FactSales[StoreKey] → DimStore[StoreKey]"], why: "the filter on agents" }],
  formatting: { target: "value", color: "#DC143C", name: "red", rule: "fixed colour for BottomN", why: "BottomN bars are red" },
  notes: ["ALL counts the (Blank) agent."], engine: "Studio's DAX engine", text: "46.76% = 137 ÷ 293",
});

test("an explanation is read with its steps, filters, joins and colour; steps without one value say so", () => {
  const found = judgement.readExplanation(explanationBody());
  assert.equal(found.formatted, "46.76%");
  assert.equal(found.steps.length, 3);
  assert.equal(found.steps[2].depth, 6);
  assert.equal(judgement.stepValueText(found.steps[0]), "137");
  assert.equal(judgement.stepValueText(found.steps[1]), "no single value here");
  assert.equal(judgement.stepValueText(found.steps[2]), "not computed");
  assert.equal(found.filters[0].reaches, false);
  assert.equal(judgement.filterSourceLabel(found.filters[0].source), "Slicer");
  assert.equal(judgement.filterSourceLabel("cross_filter"), "Selection");
  assert.deepEqual(found.relationships[0].tables, ["FactSales", "DimStore", "DimEmployee"]);
  assert.equal(found.formatting.name, "red");
  assert.equal(judgement.readExplanation("junk"), undefined);
  const points = judgement.pointChoices(chart());
  const measures = judgement.measureChoices(chart());
  assert.deepEqual(judgement.explainBody("p1", "c1", points[1], measures[1], undefined, "colour"), { page_id: "p1", visual_id: "c1", point: { values: { "Orders.Region": "East" }, measure: "Orders.Profit" }, focus: "colour" });
  assert.deepEqual(judgement.explainBody("p1", "k", undefined, undefined, { selections: [] }), { page_id: "p1", visual_id: "k", canvas: { selections: [] }, focus: "value" });
});

// --- the planner's plan ---------------------------------------------------------------------------------------------------

test("a plan card's words come from its steps: a plan without a Desktop step applies in seconds", () => {
  const steps = judgement.readPlanSteps([
    { summary: "Hide the chart", op: "set_visual_hidden", needs_desktop: false, confirm: false },
    { summary: "Rename the column", op: "rename_column", needs_desktop: true, confirm: true, outside_supported: true, verified_on: "2.158.1177.0" },
    { summary: "Hide a page", op: "set_page_hidden", needs_desktop: false, confirm: true },
    { summary: "Change the query", op: "set_table_query", needs_desktop: true, confirm: true, outside_supported: true, verified_on: null },
  ]);
  assert.deepEqual(steps.map(s => [s.outsideSupported, s.verifiedOn]), [[false, null], [true, "2.158.1177.0"], [false, null], [true, null]]);
  const report = judgement.planView(steps.slice(0, 1));
  assert.equal(report.applyLabel, "Apply to report");
  assert.doesNotMatch(report.heading, /needs Power BI/);
  const model = judgement.planView(steps.slice(1, 3));
  assert.equal(model.applyLabel, "Apply to report (needs Power BI, about 1 min)");
  assert.equal(model.blocked, false);
  assert.deepEqual(model.badges[0].labels, ["Needs Power BI", "Outside Microsoft's supported list; tested with Desktop 2.158.1177.0"]);
  assert.deepEqual(model.badges[1].labels, ["Asks first"]);
  assert.equal(judgement.planView([], true).needsDesktop, true);
  // A step no probe covers on this Desktop: Apply would be refused, and the card says so up front.
  const blocked = judgement.planView(steps.slice(3));
  assert.equal(blocked.blocked, true);
  assert.match(blocked.heading, /not verified on your Power BI Desktop/);
  assert.match(blocked.badges[0].labels[1], /not verified on your Desktop/);
  // The server decides what is outside the list, not the operation's name.
  assert.deepEqual(judgement.planView(judgement.readPlanSteps([{ summary: "x", op: "rename_column", needs_desktop: true }])).badges[0].labels, ["Needs Power BI"]);
  // Formulas are shown whole.
  const long = `SUMX(${"'Sales'[Amount] + ".repeat(80)}0)`;
  const formulas = judgement.planFormulas([{ op: "create_measure", name: "Big", expression: long }, { op: "set_table_query", table: "T", expression: "let a = 1 in a" }, { op: "create_calculation_group", name: "Time", items: [{ name: "PY", expression: "CALCULATE(SELECTEDMEASURE())" }] }, { op: "create_role", name: "R", filters: [{ table: "Store", expression: "[Type] = \"A\"" }] }]);
  assert.equal(formulas[0].expression, long);
  assert.deepEqual(formulas.map(f => f.language), ["DAX", "M", "DAX", "DAX"]);
  assert.equal(formulas.length, 4);
  assert.equal(judgement.needsDesktopOp("create_relationship"), true);
  assert.equal(judgement.needsDesktopOp("set_alt_text"), false);
});

test("chat: the plan replaces its interim confirmation, and review findings join their receipt", () => {
  const { parseChatEditEvent, addEditCard } = editing;
  const plan = parseChatEditEvent("edit_plan", { steps: [{ summary: "Add a bookmark", op: "add_bookmark", needs_desktop: false, confirm: false, source: "model" }], ops: [{ op: "add_bookmark", name: "Top agents view" }], detail: "Here is what I would do.", needs_desktop: false, left_out: ["make it pop"], pending_id: "p1" });
  assert.equal(plan.type, "plan");
  assert.equal(plan.needsDesktop, false);
  assert.deepEqual(plan.leftOut, ["make it pop"]);
  const interim = parseChatEditEvent("edit_confirm", { kind: "plan", ops: plan.ops, detail: "d", pending_id: "p1", steps: [{ summary: "Add a bookmark", op: "add_bookmark", needs_desktop: false }] });
  assert.equal(interim.steps.length, 1);
  // Plan first, then the interim card: only the plan stays.
  let cards = addEditCard(addEditCard([], plan, "a"), interim, "b");
  assert.deepEqual(cards.map(c => c.type), ["plan"]);
  // The other order: the plan replaces it.
  cards = addEditCard(addEditCard([], interim, "b"), plan, "a");
  assert.deepEqual(cards.map(c => c.type), ["plan"]);
  // Another pending id is another question.
  assert.equal(addEditCard(cards, { ...interim, pendingId: "p2" }, "c").length, 2);
  assert.equal(parseChatEditEvent("edit_plan", { steps: [], pending_id: "p" }), undefined);
  // Findings of an edit join the receipt of the same revision.
  const applied = parseChatEditEvent("edit_applied", { revision_id: "r9", head: { id: "r9" }, summaries: ["Moved it."] });
  const review = parseChatEditEvent("edit_review", { revision_id: "r9", findings: [finding()] });
  cards = addEditCard(addEditCard([], applied, "x"), review, "y");
  assert.deepEqual(cards.map(c => c.type), ["applied"]);
  assert.equal(cards[0].event.review.length, 1);
  assert.deepEqual(addEditCard([], review, "z").map(c => c.type), ["review"]);
  // A receipt that carries its review keeps it, and the duplicate event is dropped.
  const withReview = parseChatEditEvent("edit_applied", { revision_id: "r9", head: { id: "r9" }, summaries: [], receipt: { review: [finding()], tests: { text: "2 report tests still pass." }, model_health: { new: [{ message: "Hidden key column." }] } } });
  assert.deepEqual(withReview.testLines, ["2 report tests still pass."]);
  assert.equal(withReview.modelHealth.length, 1);
  assert.equal(addEditCard([{ id: "x", type: "applied", event: withReview }], review, "y").length, 1);
  const explained = parseChatEditEvent("explanation", explanationBody());
  assert.equal(explained.type, "explanation");
  const suggested = parseChatEditEvent("test_suggestions", { tests: [{ id: "a", name: "A", measure: { table: "T", name: "M" }, filters: [], expect: { equals: 1 } }], text: ["A is 1."] });
  assert.equal(suggested.suggestions.tests.length, 1);
  assert.equal(parseChatEditEvent("test_suggestions", { tests: [] }), undefined);
});

test("an answered choice shows every event of the answer: the live 'hide the StoreKey column' confirmation and the reply", async () => {
  const { parseChatEditEvent, addEditCard, readChatAnswer, foldChatAnswer } = editing;
  const question = parseChatEditEvent("edit_choices", { question: "FactSales and DimStore each have a column StoreKey. Which one should I change?", choices: [{ index: 0, label: "Hide FactSales[StoreKey] in the model" }, { index: 1, label: "Hide DimStore[StoreKey] in the model" }], pending_id: "q1" });
  let cards = addEditCard([], question, "q");
  // The exact answer of POST .../chat/edit-choice in the live app.
  const detail = "Hide DimStore[StoreKey] in the model. Applying changes to the model needs Power BI Desktop (about 1 minute).";
  const response = { status: 200, body: {
    message: "Hiding DimStore[StoreKey] needs Power BI Desktop. Choose Apply to report or Cancel.", text: "Hiding DimStore[StoreKey] needs Power BI Desktop. Choose Apply to report or Cancel.", receipt: null,
    events: [{ event: "edit_progress", data: { stage: "planning", label: "Planning" } }, { event: "edit_confirm", data: { kind: "model_edit", ops: [{ op: "set_column_properties", table: "DimStore", name: "StoreKey", hidden: true }], detail, pending_id: "c1" } }],
  } };
  const reply = readChatAnswer(response);
  assert.deepEqual(reply.events.map(event => event.type), ["confirm"]);
  let n = 0;
  const folded = foldChatAnswer(cards, "q", "Hide DimStore[StoreKey] in the model", reply, () => `n${++n}`);
  assert.equal(folded.applied, false);
  cards = folded.cards;
  assert.deepEqual(cards.map(card => card.type), ["choices", "confirm"]);
  assert.equal(cards[0].answered, "Hide DimStore[StoreKey] in the model");
  assert.equal(cards[0].reply, "Hiding DimStore[StoreKey] needs Power BI Desktop. Choose Apply to report or Cancel.");
  assert.deepEqual(cards[1].event.ops, [{ op: "set_column_properties", table: "DimStore", name: "StoreKey", hidden: true }]);
  assert.equal(cards[1].event.pendingId, "c1");
  const { EditChatCards } = await load("EditChatCards.tsx");
  const html = render(EditChatCards, { cards, headId: "r1", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop, onQueue: noop });
  assert.ok(html.includes("You chose: Hide DimStore[StoreKey] in the model"));
  assert.ok(html.includes("Choose Apply to report or Cancel."), "the reply text is shown");
  assert.ok(html.includes(detail));
  assert.ok(html.includes("Apply to report (needs Power BI, about 1 min)") && html.includes(">Cancel</button>"), "the confirmation can be answered");
  assert.ok(html.includes("Add to pending changes"), "a model change can also wait in the tray");
  // Other answers fold the same way: a plan, a receipt with its findings (not twice), a refusal that keeps the question open.
  const planned = foldChatAnswer(cards, cards[1].id, "applied", readChatAnswer({ status: 200, body: { events: [{ event: "edit_plan", data: { steps: [{ summary: "Hide the column", op: "set_column_properties", needs_desktop: true }], ops: [], detail: "d", pending_id: "p9" } }] } }), () => "x");
  assert.deepEqual(planned.cards.map(card => card.type), ["choices", "confirm", "plan"]);
  const receipt = { revision: { id: "r2", parent: "r1" }, head: { id: "r2", hash: "h" }, summaries: ["Hid DimStore[StoreKey]."], warnings: [], protected: [], visible_in: [], outcome: "updated" };
  const done = foldChatAnswer(cards, cards[1].id, "applied", readChatAnswer({ status: 200, body: { message: "Done.", receipt, events: [{ event: "edit_applied", data: { revision_id: "r2", head: { id: "r2" }, summaries: ["Hid DimStore[StoreKey]."] } }, { event: "edit_review", data: { revision_id: "r2", findings: [finding()] } }] } }), () => "y");
  assert.equal(done.applied, true);
  assert.deepEqual(done.cards.map(card => card.type), ["choices", "confirm", "applied"]);
  assert.equal(done.cards[2].event.review.length, 1);
  assert.equal(done.cards[1].answered, "applied");
  const bare = foldChatAnswer(cards, cards[1].id, "applied", readChatAnswer({ status: 200, body: { receipt } }), () => "z");
  assert.deepEqual(bare.cards.map(card => card.type), ["choices", "confirm", "applied"]);
  const busy = foldChatAnswer(cards, cards[1].id, "applied", readChatAnswer({ status: 409, body: { code: "edit_busy", message: "Another change is still being saved." } }), () => "w");
  assert.equal(busy.cards[1].answered, undefined, "the question stays open for another try");
  assert.equal(busy.cards[2].event.message, "Another change is still being saved.");
});

test("a confirmation the edit layer asks is answered on the operation that takes it; broken tests on the request", async () => {
  const { classifyEditProblem, confirmOperations, problemUi, createEditingClient, driveJob } = editing;
  const broken = classifyEditProblem(409, { code: "needs_confirmation", kind: "report_tests_broken", message: "This change would break 1 report test:\n- A is 2 now instead of 1.\nApply it anyway?", tests: [{ id: "a", name: "A", status: "failed", message: "A is 2 now instead of 1." }], confirm: { confirmed: ["report_tests_broken"] } });
  assert.equal(broken.confirm, "report_tests_broken");
  assert.equal(broken.tests.length, 1);
  assert.equal(problemUi(broken).title, "This change would break report tests");
  assert.equal((problemUi(broken).body.match(/A is 2 now instead of 1\./g) ?? []).length, 1);
  assert.equal(classifyEditProblem(409, { code: "needs_confirmation", kind: "remove_bookmark_references" }).confirm, "bookmark_references");
  assert.equal(classifyEditProblem(409, { code: "needs_confirmation", kind: "clear_page_links" }).confirm, "page_links");
  assert.equal(classifyEditProblem(409, { code: "needs_confirmation", kind: "cleanup_references" }).confirm, "cleanup_references");
  assert.deepEqual(confirmOperations([{ op: "delete_bookmark", bookmark: "B" }, { op: "set_alt_text", visual: "v" }], "bookmark_references"), [{ op: "delete_bookmark", bookmark: "B", cleanup_references: true }, { op: "set_alt_text", visual: "v" }]);
  assert.deepEqual(confirmOperations([{ op: "clear_page_type", page: "P" }], "page_links"), [{ op: "clear_page_type", page: "P", cleanup_references: true }]);
  assert.deepEqual(confirmOperations([{ op: "ungroup_visuals", group: "G" }], "cleanup_references"), [{ op: "ungroup_visuals", group: "G", cleanup_references: true }]);
  assert.deepEqual(confirmOperations([{ op: "set_conditional_format", visual: "v", style: "gradient" }], "replace_conditional")[0].replace_conditional, true);
  // "Apply anyway" re-sends the batch with `confirmed`.
  const bodies = [];
  const answers = [
    { status: 409, body: { code: "needs_confirmation", kind: "report_tests_broken", message: "Break?", tests: [] } },
    { status: 200, body: { revision: { id: "r2", parent: "r1" }, head: { id: "r2", hash: "h" }, summaries: ["Changed the measure."], warnings: [], protected: [], visible_in: [], outcome: "updated" } },
  ];
  const client = createEditingClient(async (method, path, body) => { bodies.push(body); return answers.shift(); }, { key: () => `k-${bodies.length}`, sleep: async () => undefined });
  const outcome = await driveJob(client, "w", { kind: "edit", key: "k-0", operations: [{ op: "update_measure", name: "M", expression: "2" }], options: { confirm_model: true } },
    { head: () => "r1", refreshHead: async () => "r1", decide: async () => ({ action: "replace" }) });
  assert.equal(outcome.status, "applied");
  assert.deepEqual(bodies[1].confirmed, ["report_tests_broken"]);
  assert.equal(bodies[1].confirm_model, true);
  assert.equal(bodies[1].confirm_replace, undefined);
});

test("every model-family operation needs Desktop, and round-3 layout operations re-read formatting only", () => {
  for (const op of ["create_measure", "rename_column", "create_relationship", "set_table_query", "refresh_data", "set_model_options", "create_role"]) assert.equal(editing.isModelOperation({ op }), true, op);
  for (const op of ["set_alt_text", "add_bookmark", "set_conditional_format"]) assert.equal(editing.isModelOperation({ op }), false, op);
  assert.equal(editing.isFormattingOnly([{ op: "align_visuals", visuals: ["a", "b"], edge: "left" }, { op: "add_bookmark", name: "B" }]), true);
  assert.equal(editing.isFormattingOnly([{ op: "set_conditional_format", visual: "v" }]), false);
  assert.equal(editing.isFormattingOnly([{ op: "set_table_layout", visual: "v", totals: true }]), false);
  assert.equal(editing.operationName("set_analytics_line"), "Analytics line");
});

// --- conditional formatting, analytics, alt text --------------------------------------------------------------------------

test("conditional formatting offers Power BI's targets and styles per visual, and reads what the visual has", () => {
  assert.deepEqual(extras.cfTargets({ kind: "table" }).map(t => t.value), ["background", "font_color", "data_bars", "icon", "web_url", "title_color", "visual_background"]);
  assert.deepEqual(extras.cfTargets({ kind: "column" }).map(t => t.value), ["data_colors", "title_color", "visual_background"]);
  assert.deepEqual(extras.cfTargets({ kind: "card" }).map(t => t.label), ["Value color", "Title color", "Visual background"]);
  assert.deepEqual(extras.cfTargets({ kind: "textbox" }), []);
  assert.deepEqual(extras.cfStyles("data_bars").map(s => s.value), ["data_bars"]);
  assert.deepEqual(extras.cfStyles("icon").map(s => s.value), ["icons", "rules", "field_value"]);
  assert.deepEqual(extras.cfStyles("background").map(s => s.value), ["gradient", "rules", "field_value"]);
  assert.deepEqual(extras.cfStyles("web_url").map(s => s.value), ["field_value"]);
  assert.deepEqual(extras.cfFields(table(), "background").map(f => f.value), ["Agent", "MTD Sales", "Rank"]);
  assert.deepEqual(extras.cfFields(table(), "data_bars").map(f => f.value), ["MTD Sales", "Rank"]);
  assert.deepEqual(extras.cfFields(chart(), "data_colors").map(f => f.value), ["Sales", "Profit"]);
  assert.deepEqual(extras.cfFields(chart(), "title_color"), []);
  assert.deepEqual(extras.existingRules(table()).map(r => [r.target, r.field]), [["background", "MTD Sales"], ["font_color", "Rank"], ["data_bars", "Rank"]]);
  assert.deepEqual(extras.existingRules(chart()).map(r => r.target), ["data_colors"]);
  assert.deepEqual(extras.existingRules({ kind: "card", columns: [], formatting: { cardValue: { colorIndex: 2 }, dynamicProperties: ["title.fontColor", "background.color"] } }).map(r => r.target), ["font_color", "title_color", "visual_background"]);
  assert.deepEqual(extras.clearCfOperation("t1", "data_bars", "Rank"), { op: "clear_conditional_format", visual: "t1", target: "data_bars", field: "Rank" });
});

test("a conditional formatting draft becomes one set_conditional_format, or says what is missing", () => {
  const fields = extras.cfFields(table(), "background");
  const draft = { ...extras.newCfDraft(table(), "background"), field: "MTD Sales" };
  assert.deepEqual(extras.cfOperation("t1", draft, fields).op, { op: "set_conditional_format", visual: "t1", style: "gradient", target: "background", field: "MTD Sales", min_color: "#DEEFFF", max_color: "#118DFF", blank: "as_zero" });
  const three = extras.cfOperation("t1", { ...draft, useMid: true, midValue: "50", minValue: "0", maxValue: "1,000", blank: "specific_color", blankColor: { theme: 3, percent: 0 } }, fields).op;
  assert.deepEqual([three.mid_color, three.mid_value, three.min_value, three.max_value, three.blank, three.blank_color], ["#9DC8F5", 50, 0, 1000, "specific_color", { theme: 3, percent: 0 }]);
  const rules = extras.cfOperation("t1", { ...draft, style: "rules", target: "font_color", rules: [{ min: "0", max: "50", unit: "percent", color: "#FF0000", equals: "", blank: false }, { min: "", max: "", unit: "number", color: "#00FF00", equals: "", blank: true }, { min: "", max: "", unit: "number", color: "#0000FF", equals: "West", blank: false }], applyTo: "values_and_totals" }, fields).op;
  assert.deepEqual(rules.rules, [{ color: "#FF0000", min: 0, max: 50, unit: "percent" }, { color: "#00FF00", blank: true }, { color: "#0000FF", equals: "West" }]);
  assert.equal(rules.apply_to, "values_and_totals");
  const iconRules = extras.cfOperation("t1", { ...draft, style: "rules", target: "icon", rules: [{ min: "10", max: "", unit: "number", icon: "ColoredArrowUp", equals: "", blank: false }] }, fields).op;
  assert.deepEqual([iconRules.style, iconRules.rules[0].icon, iconRules.icon_position], ["icons", "ColoredArrowUp", "left"]);
  assert.deepEqual(extras.cfOperation("t1", { ...draft, style: "field_value", target: "font_color", measure: "CF Table" }, fields).op.measure, "CF Table");
  const bars = extras.cfOperation("t1", { ...draft, style: "data_bars", target: "data_bars", field: "Rank", minValue: "", maxValue: "10", barOnly: true }, fields).op;
  assert.deepEqual([bars.positive_color, bars.negative_color, bars.max_value, bars.bar_only, bars.direction], ["#5B9BD5", "#D64550", 10, true, "left_to_right"]);
  assert.deepEqual(extras.cfOperation("t1", { ...draft, style: "icons", target: "icon", iconSet: "ThreeTrafficLightsUnrimmed", iconPosition: "icon_only" }, fields).op.icon_set, "ThreeTrafficLightsUnrimmed");
  const whole = extras.cfOperation("c1", { ...extras.newCfDraft(chart(), "title_color"), style: "field_value", measure: "Title Color", field: "Sales" }, []).op;
  assert.equal(whole.field, undefined);
  assert.equal(whole.target, "title_color");
  const problems = [
    [{ ...draft, field: undefined }, "Pick the field to format."],
    [{ ...draft, minValue: "a" }, "The values of the color scale must be numbers."],
    [{ ...draft, minValue: "10", maxValue: "5" }, "The lowest value must be below the highest."],
    [{ ...draft, blank: "specific_color", blankColor: undefined }, "Pick the color for blank values."],
    [{ ...draft, style: "rules", rules: [] }, "Add at least one rule."],
    [{ ...draft, style: "rules", rules: [{ min: "9", max: "1", unit: "number", color: "#000000", equals: "", blank: false }] }, "Rule 1: the lower bound is above the upper bound."],
    [{ ...draft, style: "rules", rules: [{ min: "0", max: "150", unit: "percent", color: "#000000", equals: "", blank: false }] }, "Rule 1: a percent is between 0 and 100."],
    [{ ...draft, style: "rules", target: "icon", rules: [{ min: "", max: "", unit: "number", equals: "", blank: false }] }, "Rule 1 has no icon."],
    [{ ...draft, style: "field_value", measure: undefined }, "Pick the measure whose values give the format."],
    [{ ...draft, style: "data_bars", minValue: "5", maxValue: "5" }, "The lowest value must be below the highest."],
  ];
  for (const [bad, message] of problems) assert.deepEqual(extras.cfOperation("t1", bad, fields), { ok: false, message });
  const moved = extras.moveRule([{ min: "1" }, { min: "2" }, { min: "3" }], 2, -1);
  assert.deepEqual(moved.map(r => r.min), ["1", "3", "2"]);
  assert.equal(extras.moveRule(moved, 0, -1), moved);
  // Every icon Studio can write has a glyph for the picker.
  assert.equal(extras.ICON_SETS.length, 25);
  for (const name of extras.ICON_NAMES) assert.ok(extras.iconGlyph(name).glyph.length > 0, name);
});

test("analytics lines: the kinds a chart offers, its lines numbered per kind, and the operations", () => {
  assert.deepEqual(extras.analyticsKinds({ kind: "column" }).map(k => k.value), ["constant", "min", "max", "average", "median", "percentile", "trend"]);
  assert.ok(extras.analyticsKinds({ kind: "line" }).some(k => k.value === "forecast"));
  assert.deepEqual(extras.analyticsKinds({ kind: "pie" }), []);
  assert.deepEqual(extras.analyticsKinds({ kind: "table" }), []);
  const lines = extras.numberLines(extras.readAnalyticsLines(chart()));
  assert.deepEqual(lines.map(l => [l.line.kind, l.number]), [["constant", 1], ["average", 1], ["average", 2]]);
  assert.deepEqual(extras.analyticsOperation("c1", { ...extras.newLineDraft("constant"), value: "50,000", name: " Target " }).op,
    { op: "set_analytics_line", visual: "c1", kind: "constant", new: true, value: 50000, name: "Target", color: "#D64550", style: "dashed", position: "front", data_label: false });
  const edited = extras.analyticsOperation("c1", extras.lineDraftOf(lines[0].line), lines[0]).op;
  assert.deepEqual([edited.line, edited.value, edited.style, edited.color, edited.new], ["Target", 4, "dotted", "#FF0000", undefined]);
  assert.equal(extras.analyticsOperation("c1", extras.lineDraftOf(lines[2].line), lines[2]).op.line, 2);
  assert.equal(extras.analyticsOperation("c1", { ...extras.newLineDraft("x_constant"), value: "2009-12-31" }).op.value, "2009-12-31");
  assert.equal(extras.analyticsOperation("c1", { ...extras.newLineDraft("percentile"), percentile: "90" }).op.percentile, 90);
  assert.equal(extras.analyticsOperation("c1", { ...extras.newLineDraft("forecast"), forecastLength: "6" }).op.forecast_length, 6);
  assert.equal(extras.analyticsOperation("c1", { ...extras.newLineDraft("forecast") }).op.style, undefined);
  for (const [draft, message] of [
    [{ ...extras.newLineDraft("constant"), value: "" }, "Give the line's value."],
    [{ ...extras.newLineDraft("constant"), value: "high" }, "The value must be a number."],
    [{ ...extras.newLineDraft("x_constant"), value: "Dec" }, "Give a number, or a date as YYYY-MM-DD."],
    [{ ...extras.newLineDraft("percentile"), percentile: "101" }, "The percentile is a number from 0 to 100."],
    [{ ...extras.newLineDraft("forecast"), forecastLength: "0" }, "Forecast how many points ahead? Give a whole number."],
  ]) assert.deepEqual(extras.analyticsOperation("c1", draft), { ok: false, message });
  assert.deepEqual(extras.removeLineOperation("c1", lines[2].line, 2), { op: "remove_analytics_line", visual: "c1", kind: "average", line: 2 });
  assert.deepEqual(extras.removeLineOperation("c1", { kind: "measure", name: "Goal" }, 1), { op: "remove_analytics_line", visual: "c1", kind: "constant", line: "Goal" });
  assert.deepEqual(extras.altTextOperation("v", "  Agents ranked  "), { op: "set_alt_text", visual: "v", text: "Agents ranked" });
  assert.deepEqual(extras.altTextOperation("v", " "), { op: "set_alt_text", visual: "v", text: null });
  assert.deepEqual(extras.visualTooltipOperation("v", "p5"), { op: "set_visual_tooltip", visual: "v", page: "p5" });
  assert.deepEqual(extras.visualTooltipOperation("v", undefined), { op: "set_visual_tooltip", visual: "v" });
  assert.equal(extras.tooltipPageOf({ tooltipPage: { page: "p5" } }), "p5");
  assert.equal(extras.hasNumbers("slicer"), false);
  assert.equal(extras.hasNumbers("card"), true);
});

test("field-scoped catalog controls get a column picker and send the column they name; column styling is not offered", () => {
  const catalog = format.readFormatCatalog({ sections: [{ id: "columns", title: "Columns", controls: [
    { id: "columns.width", object: "columnWidth", property: "value", kind: "number", writable: false, scope: "field", field_arg: "field", op: { name: "set_column_format", field: "width", fixed: {} } },
    { id: "columns.headerText", object: "query", property: "displayName", kind: "text", writable: false, scope: "field", field_arg: "field", op: { name: "set_field_display_name", field: "name", fixed: {} } },
    { id: "columns.alignment", object: "columnFormatting", property: "alignment", kind: "select", writable: false, scope: "field", op: { name: "set_column_format", field: "alignment", fixed: {} }, options: [{ value: "left", label: "Left" }] },
    { id: "columns.fontColor", object: "columnFormatting", property: "fontColor", kind: "color", writable: false, scope: "field", op: { name: "set_column_format", field: "font_color", fixed: {} } },
    { id: "columns.position", object: "query", property: "order", kind: "number", writable: false, scope: "field", op: { name: "move_column", field: "to_index", fixed: {} } },
    { id: "hidden", object: "o", property: "p", kind: "toggle", writable: false, op: { name: "set_visual_property", field: "value", fixed: {} } },
  ] }] }, "tableEx");
  const info = table();
  const sections = format.sectionsFor(info, catalog);
  const controls = sections[0].controls;
  assert.deepEqual(controls.map(c => c.id), ["columns.headerText", "columns.position"]);
  assert.ok(controls.every(c => c.fieldScoped));
  assert.deepEqual(controls[0].write(info, "Sales this month", "MTD Sales"), { op: "set_field_display_name", visual: "t1", field: "MTD Sales", name: "Sales this month" });
  assert.deepEqual(controls[1].write(info, 2, "MTD Sales"), { op: "move_column", visual: "t1", field: "MTD Sales", to_index: 2 });
  assert.equal(controls[1].write(info, 2, undefined), undefined);
  assert.equal(controls[0].read(info, "Rank"), "Rank");
  assert.equal(controls[0].read(info, undefined), undefined);
  assert.deepEqual(format.fieldColumns(info).map(o => o.value), ["Agent", "MTD Sales", "Rank"]);
});

// --- the panes --------------------------------------------------------------------------------------------------------------

test("the page render's pane facts are read: page type, bookmarks, interactions, sync groups, phone layout, layers", () => {
  const meta = panes.readPaneMeta(renderBody());
  assert.equal(meta.pageType, "drillthrough");
  assert.deepEqual(meta.binding, { type: "drillthrough", fields: [{ field: "DimStore.StoreName", table: "DimStore", column: "StoreName" }], keepAllFilters: false, crossReport: true });
  assert.deepEqual(meta.bookmarks.map(b => [b.name, b.group?.name, b.captures.data]), [["B1", undefined, true], ["B2", "G", false]]);
  assert.deepEqual(meta.interactions, { s1: { c1: "filter", c2: "highlight", c3: "none", c4: "unsupported" } });
  assert.deepEqual(meta.syncGroups.Stores, [{ page: "p1", visual: "s1", hidden: false }, { page: "p2", visual: "s9", hidden: true }]);
  assert.deepEqual(Object.keys(meta.mobile), ["c1"]);
  assert.equal(meta.drillthroughs[0].title, "Store details");
  assert.equal(meta.visuals.b1.action.type, "bookmark");
  assert.equal(meta.visuals.s1.syncGroup.groupName, "Stores");
  assert.equal(meta.visuals.c1.tooltipPage, "p5");
  assert.equal(meta.visuals.k2.hiddenOwn, true);
  // The data route lists visuals instead of keying them.
  const listed = panes.readPaneMeta({ visuals: [{ id: "a", title: "A", kind: "card" }, { title: "no id" }] });
  assert.deepEqual(Object.keys(listed.visuals), ["a"]);
  assert.equal(listed.pageType, "default");
  assert.equal(listed.mobile, undefined);
  assert.deepEqual(panes.readPaneMeta(null).bookmarks, []);
});

test("bookmarks: pane order with groups, new names, capture options and the operations of each action", () => {
  const list = bookmarks();
  const tree = panes.bookmarkTree(list);
  assert.deepEqual(tree.map(e => e.kind === "group" ? `group ${e.name} (${e.items.map(i => i.bookmark.name).join(",")})` : e.bookmark.name), ["B1", "group G1 (B2,B3)", "B4"]);
  assert.deepEqual(tree.map(e => e.position), [1, 2, 3]);
  assert.equal(panes.nextBookmarkName(list), "Bookmark 6");
  assert.equal(panes.nextBookmarkName([]), "Bookmark 1");
  assert.deepEqual(panes.addBookmarkOp(" Top view ", "p1", { data: false, display: true, currentPage: true, selectedOnly: true }, ["v1", "v2"], "G1"),
    { op: "add_bookmark", name: "Top view", page: "p1", data: false, display: true, current_page: true, visuals: ["v1", "v2"], group: "G1" });
  assert.equal(panes.addBookmarkOp(" ", "p1", panes.DEFAULT_CAPTURE, []), undefined);
  assert.deepEqual(panes.addBookmarkOp("A", "p1", { ...panes.DEFAULT_CAPTURE, selectedOnly: true }, []).visuals, undefined);
  assert.deepEqual(panes.updateBookmarkOp("B1", { data: true, selectedOnly: false }), { op: "update_bookmark", bookmark: "B1", data: true, visuals: [] });
  assert.deepEqual(panes.updateBookmarkOp("B1"), { op: "update_bookmark", bookmark: "B1" });
  // Drag: into a group, out of a group, within the top level, within a group.
  assert.deepEqual(panes.moveBookmarkOp(list, 0, 2), { op: "move_bookmark", bookmark: "B1", group: "G1", position: 2 });
  assert.deepEqual(panes.moveBookmarkOp(list, 1, 3), { op: "move_bookmark", bookmark: "B2", top_level: true, position: 3 });
  assert.deepEqual(panes.moveBookmarkOp(list, 3, 0), { op: "move_bookmark", bookmark: "B4", position: 1 });
  assert.deepEqual(panes.moveBookmarkOp(list, 2, 1), { op: "move_bookmark", bookmark: "B3", position: 1 });
  assert.equal(panes.moveBookmarkOp(list, 1, 1), undefined);
  assert.deepEqual(panes.nudgeBookmarkOp(list, "B3", -1), { op: "move_bookmark", bookmark: "B3", position: 1 });
  assert.equal(panes.nudgeBookmarkOp(list, "B2", -1), undefined);
  assert.deepEqual(panes.nudgeBookmarkOp(list, "B4", -1), { op: "move_bookmark", bookmark: "B4", position: 2 });
  assert.equal(panes.nudgeBookmarkOp(list, "B4", 1), undefined);
  const meta = panes.readPaneMeta(renderBody());
  assert.deepEqual(panes.bookmarkUsers({ name: "B1", displayName: "Overview" }, meta.visuals).map(v => v.id), ["b1"]);
  assert.deepEqual(panes.bookmarkUsers({ name: "B9", displayName: "Other" }, meta.visuals), []);
  // Hide in bookmark.
  assert.equal(panes.bookmarkHides(list[0], "v2"), true);
  assert.equal(panes.bookmarkHides(list[0], "v1"), false);
  assert.equal(panes.bookmarkHides(list[0], "v9"), false);
  assert.equal(panes.bookmarkHides(list[1], "v3"), true);
  assert.equal(panes.bookmarkHides(list[1], "v9"), undefined);
  assert.equal(panes.bookmarkHides(list[2], "v1"), undefined);
});

test("layers, interactions and synced slicers", () => {
  const meta = panes.readPaneMeta(renderBody());
  const rows = panes.layerRows(meta.visuals, [{ id: "extra", title: "From the layout", kind: "card", hidden: false }]);
  assert.deepEqual(rows.map(r => `${"  ".repeat(r.depth)}${r.id}`), ["c1", "g1", "  k2", "  k1", "s1", "b1", "extra"]);
  assert.deepEqual(panes.interactionChoices("column"), ["filter", "highlight", "none"]);
  assert.deepEqual(panes.interactionChoices("card"), ["filter", "none"]);
  assert.deepEqual(panes.interactionChoices("textbox"), []);
  assert.equal(panes.canFilterOthers("slicer"), true);
  assert.equal(panes.canFilterOthers("card"), false);
  assert.equal(panes.interactionOf(meta.interactions, "s1", "c2"), "highlight");
  assert.equal(panes.interactionOf(meta.interactions, "x", "c2"), undefined);
  assert.deepEqual(panes.setInteractionOp("s1", "c1", "none"), { op: "set_interaction", source: "s1", targets: ["c1"], type: "none" });
  const matrix = panes.syncMatrix([{ id: "s1", title: "Store", syncGroup: "Stores" }, { id: "s2", title: "Channel" }], [{ id: "p1" }, { id: "p2" }, { id: "p3" }], "p1", meta.syncGroups);
  assert.deepEqual(matrix[0].cells, { p1: { synced: true, visible: true, own: true }, p2: { synced: true, visible: false, own: false }, p3: { synced: false, visible: false, own: false } });
  assert.deepEqual(matrix[1].cells.p2, { synced: false, visible: false, own: false });
  assert.deepEqual(panes.syncOp(matrix[0], "p3", "sync"), { op: "sync_slicer", visual: "s1", pages: ["p3"] });
  assert.deepEqual(panes.syncOp(matrix[0], "p2", "show"), { op: "sync_slicer", visual: "s1", pages: ["p2"] });
  assert.deepEqual(panes.syncOp(matrix[0], "p2", "hide"), { op: "sync_slicer", visual: "s1", hidden_on: ["p2"] });
  assert.deepEqual(panes.syncOp(matrix[0], "p2", "unsync"), { op: "unsync_slicer", visual: "s1", pages: ["p2"] });
  assert.equal(panes.syncOp(matrix[0], "p1", "unsync"), undefined);
  assert.equal(panes.syncOp(matrix[0], "p2", "sync"), undefined);
});

test("phone layout, page type, insert menu and button actions", () => {
  assert.deepEqual(panes.phoneBox({ x: 300, y: -4 }, { width: 124, height: 58 }), { x: 200, y: 0, width: 120, height: 60 });
  assert.deepEqual(panes.phoneBox({ x: 0, y: 0 }, { width: 900, height: 5 }), { x: 0, y: 0, width: 320, height: 20 });
  assert.deepEqual(panes.phoneSize({ width: 640, height: 400 }), { width: 320, height: 200 });
  assert.deepEqual(panes.phoneSize({ width: 100, height: 1000 }), { width: 320, height: 400 });
  assert.deepEqual(panes.phoneSize(undefined), { width: 320, height: 120 });
  assert.equal(panes.nextPhoneY({ a: { x: 0, y: 0, width: 320, height: 200 }, b: { x: 0, y: 210, width: 160, height: 50 } }), 270);
  assert.equal(panes.nextPhoneY(undefined), 0);
  assert.deepEqual(panes.placeOnMobileOp("v", { x: 0, y: 10, width: 320, height: 60 }), { op: "place_on_mobile", visual: "v", x: 0, y: 10, width: 320, height: 60 });
  assert.deepEqual(panes.pageTypeOp("p", "default"), { op: "clear_page_type", page: "p" });
  assert.deepEqual(panes.pageTypeOp("p", "drillthrough", { fields: [{ table: "DimStore", name: "StoreName" }], keepAllFilters: false }), { op: "set_drillthrough", page: "p", fields: [{ table: "DimStore", name: "StoreName" }], mode: "replace", keep_all_filters: false });
  assert.equal(panes.pageTypeOp("p", "drillthrough", { fields: [] }), undefined);
  assert.deepEqual(panes.pageTypeOp("p", "tooltip"), { op: "set_tooltip_page", page: "p", size: "single_visual" });
  assert.deepEqual(panes.columnRef({ field: "DimStore.StoreName" }), { table: "DimStore", name: "StoreName" });
  assert.deepEqual(panes.columnRef({ field: "x", table: "T", column: "C" }), { table: "T", name: "C" });
  assert.deepEqual(panes.insertOp("p", { kind: "button", button: "back" }), { op: "add_button", page: "p", kind: "back" });
  assert.deepEqual(panes.insertOp("p", { kind: "navigator", navigator: "bookmark" }), { op: "add_navigator", page: "p", kind: "bookmark" });
  assert.deepEqual(panes.insertOp("p", { kind: "shape", shape: "oval" }), { op: "add_shape", page: "p", shape: "oval" });
  assert.deepEqual(panes.insertOp("p", { kind: "textbox" }), { op: "add_textbox", page: "p", text: "Text" });
  assert.ok(panes.SHAPES.every(shape => /^[a-z][A-Za-z]+$/.test(shape.value)));
  assert.deepEqual(panes.actionDraft({ type: "url", url: "https://x", valid: true, tooltip: "Go" }), { type: "web_url", page: undefined, bookmark: undefined, url: "https://x", tooltip: "Go" });
  assert.deepEqual(panes.actionDraft(undefined), { type: "none", tooltip: "" });
  assert.deepEqual(panes.actionOp("b", { type: "page", page: "p2", tooltip: " Next " }).op, { op: "set_button_action", visual: "b", action: { type: "page", page: "p2" }, tooltip: "Next" });
  assert.deepEqual(panes.actionOp("b", { type: "bookmark", bookmark: "B1", tooltip: "" }).op, { op: "set_button_action", visual: "b", action: { type: "bookmark", bookmark: "B1" } });
  assert.deepEqual(panes.actionOp("b", { type: "none", tooltip: "" }).op.action, { type: "none" });
  // An empty tooltip box keeps the tooltip; "Remove tooltip" sends "".
  assert.equal("tooltip" in panes.actionOp("b", { type: "back", tooltip: "  " }).op, false);
  assert.deepEqual(panes.removeTooltipOp("b"), { op: "set_button_action", visual: "b", tooltip: "" });
  assert.deepEqual(panes.actionOp("b", { type: "web_url", url: "javascript:alert(1)", tooltip: "" }), { ok: false, message: "Give a web address that starts with http:// or https://." });
  assert.deepEqual(panes.actionOp("b", { type: "page", tooltip: "" }), { ok: false, message: "Pick the page to go to." });
  assert.deepEqual(panes.actionOp("b", { type: "drillthrough", tooltip: "" }), { ok: false, message: "Pick the drillthrough page." });
  assert.deepEqual(panes.actionOp("b", { type: "bookmark", tooltip: "" }), { ok: false, message: "Pick the bookmark." });
});

test("the canvas context menu follows the selection and its choices become operations", () => {
  const meta = panes.readPaneMeta(renderBody());
  const ids = items => items.filter(item => !item.separator).map(item => item.id);
  assert.deepEqual(ids(panes.contextMenuItems(["c1"], meta.visuals, { number: true, locked: false, testsAvailable: true })), ["explain", "pin", "interactions", "arrange:front", "arrange:back", "duplicate", "delete"]);
  assert.deepEqual(ids(panes.contextMenuItems(["k1"], meta.visuals, { number: false, locked: false, testsAvailable: true })), ["ungroup", "interactions", "arrange:front", "arrange:back", "duplicate", "delete"]);
  const two = panes.contextMenuItems(["k1", "k2"], meta.visuals, { number: false, locked: false, testsAvailable: true });
  assert.deepEqual(ids(two), ["group", "align", "swap", "place", "arrange:front", "arrange:back", "duplicate", "delete"]);
  assert.equal(two.find(i => i.id === "group").disabled, false);
  assert.equal(two.find(i => i.id === "align").children.find(c => c.id === "distribute:horizontal").disabled, true);
  assert.equal(panes.contextMenuItems(["k1", "c1"], meta.visuals, { number: false, locked: false, testsAvailable: true }).find(i => i.id === "group").disabled, true);
  assert.equal(panes.contextMenuItems(["c1"], meta.visuals, { number: true, locked: false, testsAvailable: false }).find(i => i.id === "pin").disabled, true);
  assert.equal(panes.contextMenuItems(["k1"], meta.visuals, { number: false, locked: false, testsAvailable: true }).find(i => i.id === "interactions").disabled, true);
  assert.ok(panes.contextMenuItems(["c1"], meta.visuals, { number: false, locked: true, testsAvailable: true }).filter(i => i.id === "delete" || i.id === "duplicate").every(i => i.disabled));
  assert.deepEqual(panes.menuOperations("align:left", ["a", "b"], meta.visuals), [{ op: "align_visuals", visuals: ["a", "b"], edge: "left", to: "selection" }]);
  assert.deepEqual(panes.menuOperations("distribute:vertical", ["a", "b", "c"], meta.visuals), [{ op: "distribute_visuals", visuals: ["a", "b", "c"], axis: "vertical" }]);
  assert.deepEqual(panes.menuOperations("swap", ["a", "b"], meta.visuals), [{ op: "swap_visuals", a: "a", b: "b" }]);
  assert.deepEqual(panes.menuOperations("place:below", ["a", "b"], meta.visuals), [{ op: "place_visual", visual: "a", relative_to: "b", side: "below", gap: 10 }]);
  assert.deepEqual(panes.menuOperations("group", ["k1", "k2"], meta.visuals), [{ op: "group_visuals", visuals: ["k1", "k2"] }]);
  assert.deepEqual(panes.menuOperations("ungroup", ["k1"], meta.visuals), [{ op: "ungroup_visuals", group: "g1" }]);
  assert.deepEqual(panes.menuOperations("ungroup", ["g1"], meta.visuals), [{ op: "ungroup_visuals", group: "g1" }]);
  assert.equal(panes.menuOperations("ungroup", ["c1"], meta.visuals), undefined);
  assert.deepEqual(panes.menuOperations("arrange:back", ["c1"], meta.visuals), [{ op: "arrange", visual: "c1", to: "back" }]);
  assert.deepEqual(panes.menuOperations("delete", ["a", "b"], meta.visuals), [{ op: "delete_visual", visual: "a" }, { op: "delete_visual", visual: "b" }]);
  assert.equal(panes.menuOperations("explain", ["a"], meta.visuals), undefined);
  const rects = [{ id: "low", x: 0, y: 0, width: 100, height: 100, rank: 1, hidden: false, kind: "card" }, { id: "top", x: 50, y: 50, width: 100, height: 100, rank: 2, hidden: false, kind: "card" }, { id: "g", x: 0, y: 0, width: 200, height: 200, rank: 9, hidden: false, kind: "group" }, { id: "ghost", x: 0, y: 0, width: 200, height: 200, rank: 8, hidden: true, kind: "card" }];
  assert.equal(panes.visualAt(rects, { x: 60, y: 60 }), "top");
  assert.equal(panes.visualAt(rects, { x: 10, y: 10 }), "low");
  assert.equal(panes.visualAt(rects, { x: 190, y: 190 }), undefined);
});

// --- components (server-side renders) --------------------------------------------------------------------------------------------

test("chat cards: the plan with numbered steps, badges and full DAX; confirmations worded by what they ask", async () => {
  const { EditChatCards, confirmCopy } = await load("EditChatCards.tsx");
  const { parseChatEditEvent } = editing;
  const long = `CALCULATE([Sales], ${"FILTER(ALL('Date'), 'Date'[Year] = 2009), ".repeat(12)}TRUE())`;
  const plan = parseChatEditEvent("edit_plan", { steps: [{ summary: "Make the Rank column bars", op: "set_conditional_format", needs_desktop: false }, { summary: "Rename the column StoreName", op: "rename_column", needs_desktop: true, confirm: true, outside_supported: true, verified_on: "2.158.1177.0" }, { summary: "Create the measure Big", op: "create_measure", needs_desktop: true, outside_supported: false }],
    ops: [{ op: "set_conditional_format", visual: "t" }, { op: "rename_column", table: "DimStore", name: "StoreName", new_name: "Store" }, { op: "create_measure", table: "M", name: "Big", expression: long }], detail: "Here is what I would do (3 steps).", needs_desktop: true, left_out: [], pending_id: "p1" });
  const reportOnly = parseChatEditEvent("edit_plan", { steps: [{ summary: "Add a bookmark called Top agents view", op: "add_bookmark", needs_desktop: false }], ops: [{ op: "add_bookmark", name: "Top agents view" }], detail: "d", pending_id: "p2" });
  const html = render(EditChatCards, { cards: [{ id: "a", type: "plan", event: plan }, { id: "b", type: "plan", event: reportOnly }], headId: "r1", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop });
  assert.equal((html.match(/Outside Microsoft/g) ?? []).length, 1, "only the step the server marks");
  assert.ok(html.includes("<ol class=\"ie-plan-steps\">") && html.includes("Make the Rank column bars") && html.includes("Rename the column StoreName"));
  assert.ok(html.includes("Outside Microsoft&#x27;s supported list; tested with Desktop 2.158.1177.0"));
  assert.ok(html.includes("Needs Power BI"));
  assert.ok(html.includes(long.replaceAll("'", "&#x27;")), "the whole formula is shown");
  assert.ok(html.includes("Apply to report (needs Power BI, about 1 min)"));
  // The report-only plan applies in seconds and says so.
  assert.match(html, /<button[^>]*class="primary yellow"[^>]*>Apply to report<\/button>/);
  // The interim card of a report-only plan no longer says it needs Power BI.
  const interim = { id: "c", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind: "plan", ops: [{ op: "set_visual_hidden", visual: "v", hidden: true }], detail: "d", pending_id: "p3", steps: [{ summary: "Hide the chart", op: "set_visual_hidden", needs_desktop: false }] }) };
  assert.deepEqual(confirmCopy(interim), { heading: "Planned change. Nothing changes until you apply it.", apply: "Apply to report", cancel: "Cancel" });
  const interimModel = { id: "d", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind: "plan", ops: [{ op: "create_measure", name: "X", expression: "1" }], detail: "d", pending_id: "p4" }) };
  assert.equal(confirmCopy(interimModel).apply, "Apply to report (needs Power BI, about 1 min)");
  const tests = { id: "e", type: "confirm", event: parseChatEditEvent("edit_confirm", { kind: "report_tests_broken", ops: [{ op: "update_measure", name: "Total Sales", expression: "2*[x]" }], detail: "This change would break 1 report test.", pending_id: "p5", tests: [{ id: "t", name: "A", status: "failed", message: "Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80." }] }) };
  const testsHtml = render(EditChatCards, { cards: [interim, tests], headId: "r1", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop });
  assert.ok(testsHtml.includes("Apply anyway") && testsHtml.includes("Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80.") && testsHtml.includes("This change would break report tests."));
  assert.ok(!testsHtml.includes("This needs Power BI"));
  // The edit layer's reference questions, which the chat now answers with `cleanup_references`.
  const ask = kind => confirmCopy({ id: kind, type: "confirm", event: parseChatEditEvent("edit_confirm", { kind, ops: [], detail: "d", pending_id: kind }) });
  assert.equal(ask("remove_bookmark_references").apply, "Remove and clean up");
  assert.equal(ask("clear_page_links").apply, "Remove the links and continue");
  assert.equal(ask("cleanup_references").apply, "Ungroup");
  assert.equal(ask("remove_page_references").heading, "Buttons or bookmarks point at this page.");
});

test("chat cards: a receipt shows what Studio noticed with Fix, an explanation, and suggested tests", async () => {
  const { EditChatCards } = await load("EditChatCards.tsx");
  const { parseChatEditEvent } = editing;
  const applied = parseChatEditEvent("edit_applied", { revision_id: "r2", head: { id: "r2" }, summaries: ["Moved the chart."], receipt: { review: [finding(), finding({ id: "p", severity: "problem", fix: null, message: "Two slicers filter one field." })], tests: { text: "2 report tests still pass." } } });
  const explanation = parseChatEditEvent("explanation", explanationBody());
  const suggestions = parseChatEditEvent("test_suggestions", { tests: [{ id: "s1", name: "Agents Avg MTD Sales", measure: { table: "M", name: "Avg" }, filters: [], expect: { equals: 73514.8 } }], text: ["Agents Avg MTD Sales is $73,514.80."] });
  const html = render(EditChatCards, { cards: [{ id: "a", type: "applied", event: applied, fixed: [] }, { id: "b", type: "explanation", event: explanation }, { id: "c", type: "suggestions", event: suggestions, added: [] }], headId: "r2", disabled: false,
    onUndo: noop, onChoice: noop, onConfirm: noop, onFix: noop, onAddSuggestion: noop });
  assert.ok(html.includes("Studio noticed") && html.includes("Move it 1.4 px so their left edges line up.") && html.includes("Two slicers filter one field."));
  assert.equal((html.match(/>Fix<\/button>/g) ?? []).length, 1, "a finding without a fix has no Fix button");
  assert.ok(html.includes("2 report tests still pass."));
  assert.ok(html.includes("46.76%") && html.includes("It does not change this number.") && html.includes("FactSales") && html.includes("red"));
  assert.ok(html.includes("Tests worth pinning") && html.includes("Agents Avg MTD Sales is $73,514.80.") && html.includes(">Add</button>"));
  const fixed = render(EditChatCards, { cards: [{ id: "a", type: "applied", event: applied, fixed: ["near_miss-3f2a9c01be"] }], headId: "r2", disabled: false, onUndo: noop, onChoice: noop, onConfirm: noop, onFix: noop });
  assert.ok(fixed.includes(">Fixed<"));
});

test("the new panes render: bookmarks, selection with hide in bookmark, sync slicers, tests, review, explain", async () => {
  const { BookmarksPane } = await load("BookmarksPane.tsx");
  const meta = panes.readPaneMeta(renderBody());
  const bookmarkHtml = render(BookmarksPane, { bookmarks: bookmarks(), pageId: "p1", selected: ["v1"], visuals: meta.visuals, disabled: false, onEdit: async () => true, onApply: noop, applied: "B1" });
  assert.ok(bookmarkHtml.includes("Overview") && bookmarkHtml.includes("Views") && bookmarkHtml.includes("aria-label=\"Ungroup Views\""));
  assert.ok(bookmarkHtml.includes("aria-label=\"Stop showing Overview\"") && bookmarkHtml.includes("aria-label=\"Move Top agents up\"") && bookmarkHtml.includes("Add bookmark"));
  assert.ok(render(BookmarksPane, { bookmarks: [], pageId: "p1", selected: [], visuals: {}, disabled: false, onEdit: async () => true }).includes("No bookmarks yet"));
  const { SelectionList } = await load("SelectionList.tsx");
  const layers = panes.layerRows(meta.visuals);
  const selection = render(SelectionList, { visuals: [], layers, selectedIds: ["k1"], disabled: false, bookmarks: bookmarks(), onSelect: noop, onHidden: noop, onArrange: noop, onBookmarkHidden: noop, onGroup: noop, onUngroup: noop, onTabOrder: noop });
  assert.ok(selection.includes("Visuals on this page (5)") && selection.includes("Tab order by position") && selection.includes("Bookmark: Overview"));
  assert.ok(selection.includes("aria-label=\"Show Card 2\"") && selection.includes("padding-left:14px"));
  const { SyncSlicersPane } = await load("SyncSlicersPane.tsx");
  const sync = render(SyncSlicersPane, { pageId: "p1", pages: [{ id: "p1", title: "Overview" }, { id: "p2", title: "Stores" }], visuals: meta.visuals, groups: meta.syncGroups, disabled: false, onEdit: async () => true });
  assert.ok(sync.includes("Sync Store on Stores") && sync.includes("Group “Stores”") && sync.includes("(this page)"));
  assert.match(sync, /aria-label="Show Store on Stores"(?![^>]*checked)/);
  assert.ok(render(SyncSlicersPane, { pageId: "p1", pages: [], visuals: {}, groups: {}, disabled: false, onEdit: async () => true }).includes("This page has no slicers"));
  const { TestsPane } = await load("TestsPane.tsx");
  const state = judgement.readTestsState(testsBody());
  const testsHtml = render(TestsPane, { workspaceId: "w", pageId: "p1", tests: { state, loading: false, running: false, reload: async () => undefined, run: async () => undefined, setState: noop }, client: {}, measures: [], columns: [], disabled: false });
  assert.ok(testsHtml.includes("Agents Avg MTD Sales") && testsHtml.includes("Passed") && testsHtml.includes("Failed") && testsHtml.includes("R is 150 now instead of between 0 and 100."));
  assert.ok(testsHtml.includes("The last run was for an earlier version of the report"));
  assert.ok(testsHtml.includes("Run tests") && testsHtml.includes("Suggest"));
  const { ReviewPanel } = await load("ReviewPanel.tsx");
  const review = judgement.readReview({ findings: [finding(), finding({ id: "p", severity: "problem", fix: null, message: "Off the page." })], skipped: { empty_visual: "Needs the page's data." }, notes: [] });
  const reviewHtml = render(ReviewPanel, { review, loading: false, fixed: ["near_miss-3f2a9c01be"], disabled: false, onFix: noop, onShow: noop, onRefresh: noop, onClose: noop });
  assert.ok(reviewHtml.includes("Problems") && reviewHtml.includes("Suggestions") && reviewHtml.includes("Off the page.") && reviewHtml.includes(">Fixed<") && reviewHtml.includes("Needs the page&#x27;s data."));
  const { ExplainView } = await load("ExplainPanel.tsx");
  const explainHtml = render(ExplainView, { explanation: judgement.readExplanation(explanationBody()) });
  assert.ok(explainHtml.includes("VAR _A = ...") && explainHtml.includes("no single value here") && explainHtml.includes("Slicer") && explainHtml.includes("No AI model made these figures."));
});

test("the Visual pane adds conditional formatting, analytics, action, tooltip and accessibility sections", async () => {
  const { FormatPane } = await load("FormatPane.tsx");
  const info = { ...chart(), rect: { x: 0, y: 0, width: 400, height: 240 } };
  const html = render(FormatPane, { info, disabled: false, onEdit: noop, context: { row: chart(), measures: [{ table: "M", name: "Color" }], pages: [{ id: "p2", title: "Two" }], tooltipPages: [{ id: "p2", title: "Two" }] } });
  for (const title of ["Conditional formatting", "Analytics", "Tooltip", "Accessibility", "Data colors by rule", "Target", "Y-axis constant line"]) assert.ok(html.includes(title), title);
  // Conditional formatting comes first and open.
  assert.match(html, /^<div class="ie-format"[^>]*><details class="ie-section" open=""><summary>Conditional formatting<\/summary>/);
  assert.ok(!html.includes("Format painter"));
  const tableHtml = render(FormatPane, { info: { ...table(), rect: { x: 0, y: 0, width: 400, height: 240 } }, disabled: false, onEdit: noop, context: {} });
  assert.ok(tableHtml.includes("Background of MTD Sales") && tableHtml.includes("Data bars on Rank") && !tableHtml.includes(">Analytics<"));
  const button = render(FormatPane, { info: { id: "b1", kind: "button", visualType: "actionButton", formatting: {} }, disabled: false, onEdit: noop, context: { pages: [{ id: "p2", title: "Two" }], bookmarks: [{ name: "B1", displayName: "Overview" }], layer: panes.readPaneMeta(renderBody()).visuals.b1, altText: "Opens the overview" } });
  assert.ok(button.includes("Action") && button.includes("Page navigation") && button.includes("Overview"));
  // A button's action stays; its style (text, fill and colors per state) is not edited in Studio.
  assert.ok(!button.includes("On hover") && !button.includes("Fill"));
  const navigator = render(FormatPane, { info: { id: "n1", kind: "navigator", visualType: "pageNavigator", formatting: {} }, disabled: false, onEdit: noop, context: { pages: [{ id: "p2", title: "Two" }] } });
  assert.ok(!navigator.includes("<summary>Action</summary>") && !navigator.includes("On hover"));
  assert.ok(button.includes("Remove tooltip"), "the button has a tooltip");
  assert.ok(button.includes(">Opens the overview</textarea>"), "alt text is prefilled from the report payload");
  // The Tooltip picker lists the tooltip pages it is given, plus the page the visual still uses.
  const tip = render(FormatPane, { info, disabled: false, onEdit: noop, context: { row: { ...chart(), tooltipPage: { page: "p5", title: "Old" } }, tooltipPages: [{ id: "p7", title: "Agent tooltip" }] } });
  assert.ok(tip.includes("Page: Agent tooltip") && tip.includes("(not a tooltip page any more)"));
});

test("page settings offer the page type, the phone layout and the page view", async () => {
  const { PageTabs } = await load("PageTabs.tsx");
  const html = render(PageTabs, { pages: [{ id: "p1", title: "Overview", width: 1280, height: 720, visuals: [] }], pageId: "p1", disabled: false, onSelect: noop, onEdit: async () => true,
    pageType: "drillthrough", binding: { type: "drillthrough", fields: [{ field: "DimStore.StoreName", table: "DimStore", column: "StoreName" }], keepAllFilters: true, crossReport: false },
    columns: [{ table: "DimStore", name: "StoreName" }, { table: "Date", name: "Year" }], onMobile: noop });
  assert.ok(html.includes("Page type") && html.includes("Drillthrough page") && html.includes("DimStore[StoreName]") && html.includes("Keep all filters"));
  assert.ok(html.includes("Phone layout…") && html.includes("Fit to width"));
});
