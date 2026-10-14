import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("../src/reviewChecklist.ts", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { buildReviewChecklist, canApproveDraft, pageReviewIssues, EXECUTIVE_OVERVIEW_PROMPT, defaultRelationshipSelection } = loaded.exports;

test("verified joins start included; blocked, rejected and unverified ones do not", () => {
  const base = { cardinality: "many_to_one", is_active: true, blockers: [], evidence: [], review_status: "pending", recommended_by_model: false, rely_on_referential_integrity: false };
  const selected = defaultRelationshipSelection([
    { ...base, id: "people", rely_on_referential_integrity: true },
    { ...base, id: "returns", evidence: ["Subset table: all 296 Returns key(s) exist in Orders"] },
    { ...base, id: "orphans", evidence: ["Warning: 3 orphan rows"] },
    { ...base, id: "blocked", rely_on_referential_integrity: true, blockers: ["types differ"] },
    { ...base, id: "rejected", rely_on_referential_integrity: true, review_status: "rejected" },
    { ...base, id: "inactive", rely_on_referential_integrity: true, is_active: false },
    { ...base, id: "recommended", recommended_by_model: true },
  ]);
  assert.deepEqual([...selected].sort(), ["people", "recommended", "returns"]);
});

const draft = {
  pages: [{ id: "overview", title: "Overview", purpose: "KPIs", visuals: [{ id: "kpi", recipe: "kpi", title: "Sales", measure_id: "sales", detail_column_ids: [] }], recommended_relationship_ids: [] }],
  measure_proposal: { measures: [{ tier: "governed", id: "sales", name: "Sales", operation: "sum", table_id: "orders", period: "month", format_string: "#,#0.00" }] },
  semantic_model: { approved: false, tables: [], relationships: [] },
  status: "draft",
};

test("executive first-run prompt is one page", () => {
  assert.match(EXECUTIVE_OVERVIEW_PROMPT, /one executive overview page/);
});

test("checklist is ready when pages, measures and relationships are clear", () => {
  const items = buildReviewChecklist(draft, new Set());
  assert.equal(items.find((item) => item.id === "pages").status, "ready");
  assert.equal(items.find((item) => item.id === "advanced_dax").status, "ready");
  assert.equal(canApproveDraft(items), true);
});

test("unapproved advanced DAX blocks approve", () => {
  const items = buildReviewChecklist({
    ...draft,
    measure_proposal: { measures: [{ ...draft.measure_proposal.measures[0], tier: "advanced_dax", extra_approval_required: true, approved: false, expression: "1", dependencies: [], assumptions: [] }] },
  }, new Set());
  assert.equal(items.find((item) => item.id === "advanced_dax").status, "action");
  assert.equal(canApproveDraft(items), false);
});

test("page issues block approve and unsaved edits are explained", () => {
  assert.ok(pageReviewIssues([{ ...draft.pages[0], title: "" }]).length);
  const items = buildReviewChecklist({ ...draft, pages: [] }, new Set());
  assert.equal(items.find((item) => item.id === "pages").status, "action");
  assert.match(canApproveDraft(items, { designerDirty: true }) ? "" : "blocked", /blocked|^$/);
  assert.equal(canApproveDraft(items, { designerDirty: true }), false);
});

test("a blocked candidate is excluded without blocking approval", () => {
  const blocked = { id: "rel_bad", from_table_id: "orders", to_table_id: "returns", blockers: ["No non-null key values match"], review_status: "pending" };
  const items = buildReviewChecklist({ ...draft, semantic_model: { ...draft.semantic_model, relationships: [blocked] } }, new Set());
  const relationships = items.find((item) => item.id === "relationships");
  assert.equal(relationships.status, "ready");
  assert.match(relationships.detail, /1 blocked relationship will be excluded/);
  assert.equal(canApproveDraft(items), true);
});

test("a cross-table visual needs its relationship included before approval", () => {
  const relationship = { id: "rel_sales_products", from_table_id: "orders", to_table_id: "products", blockers: [], review_status: "pending" };
  const crossTable = {
    ...draft,
    pages: [{ ...draft.pages[0], visuals: [{ id: "by_category", recipe: "category", title: "Sales by Category", measure_id: "sales", table_id: "products", category_column_id: "category", detail_column_ids: [] }] }],
    semantic_model: { ...draft.semantic_model, relationships: [relationship] },
  };
  const missing = buildReviewChecklist(crossTable, new Set());
  assert.equal(missing.find((item) => item.id === "relationships").status, "action");
  assert.match(missing.find((item) => item.id === "relationships").detail, /Sales by Category/);
  assert.equal(canApproveDraft(missing), false);
  const included = buildReviewChecklist(crossTable, new Set([relationship.id]));
  assert.equal(included.find((item) => item.id === "relationships").status, "ready");
  assert.equal(canApproveDraft(included), true);
});
