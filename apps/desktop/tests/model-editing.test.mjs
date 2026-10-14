import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

// One bundle for every module under test, so the components and the tray share one pending store.
async function loadAll(contents) {
  const compiled = await build({
    stdin: { contents, resolveDir: fileURLToPath(new URL("../src/", import.meta.url)), loader: "ts" },
    bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" }, logLevel: "silent",
  });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
  return loaded.exports;
}
const lib = await loadAll(`
  export * from "./modelEditing";
  export { pendingStore, createPendingStore } from "./importedEditing";
  export { PendingModelChanges } from "./PendingModelChanges";
  export { SupportNote, OrderedList, DeleteConfirm, CheckedQueue } from "./ModelEditorShared";
  export { ModelEditorTables } from "./ModelEditorTables";
  export { ModelEditorRelationships, diagramLayout } from "./ModelEditorRelationships";
  export { ModelEditorHierarchies, ModelEditorFieldParameters, ModelEditorCalculationGroups, ModelEditorRoles } from "./ModelEditorObjects";
  export { ModelEditorQuery, ModelEditorSettings, parameterValue, QUERY_CHECK_DELAY_MS } from "./ModelEditorQuery";
  export { ModelEditorHealth } from "./ModelEditorHealth";
  export { SaveImportedCalculation } from "./ModelEditorDaxSave";
`);
const { renderToStaticMarkup } = await import("react-dom/server");
const React = await import("react");
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));

// A model shaped like the Agents dashboard's facts (docs/imported-report-editing-api.md, "The model").
const PAYLOAD = {
  status: "current", version: 1, truncated: ["partitions"],
  model: { culture: "en-US", discourageImplicitMeasures: false, autoDateTime: false, defaultMode: "import" },
  tables: [
    { name: "FactSales", hidden: false, source: "m", calculated: false },
    { name: "DimStore", hidden: false, source: "m", calculated: false, description: "Stores" },
    { name: "DimEmployee", hidden: false, source: "m", calculated: false },
    { name: "Date", hidden: false, source: "calculated", calculated: true, expression: "CALENDARAUTO()", isDateTable: true, dateColumn: "Date" },
    { name: "Time Calc", hidden: false, source: "calculationGroup", calculated: false, calculationGroup: true },
    { name: "Sales Metric", hidden: false, source: "calculated", calculated: true, expression: "{(\"Avg\", NAMEOF('# Measures'[Avg]), 0)}" },
    { name: "LocalDateTable_1", hidden: true, calculated: true, autoDateTable: "local" },
  ],
  columns: [
    { table: "FactSales", name: "SalesAmount", kind: "data", dataTypeName: "double", summarizeBy: "sum", hidden: false, key: false },
    { table: "FactSales", name: "StoreKey", kind: "data", dataTypeName: "integer", summarizeBy: "sum", hidden: false, key: false },
    { table: "FactSales", name: "DateKey", kind: "data", dataTypeName: "datetime", summarizeBy: "none", hidden: false, key: false },
    { table: "FactSales", name: "Margin", kind: "calculated", dataTypeName: "double", expression: "FactSales[SalesAmount] * 0.2", summarizeBy: "sum", hidden: true, key: false },
    { table: "DimStore", name: "StoreKey", kind: "data", dataTypeName: "integer", summarizeBy: "none", hidden: false, key: false },
    { table: "DimStore", name: "StoreName", kind: "data", dataTypeName: "text", summarizeBy: "none", hidden: false, key: false, dataCategory: "WebUrl", formatString: "" },
    { table: "DimStore", name: "Status", kind: "data", dataTypeName: "text", hidden: false, key: false },
    { table: "DimStore", name: "Month", kind: "data", dataTypeName: "text", sortByColumn: "Month Number", hidden: false, key: false },
    { table: "DimStore", name: "Month Number", kind: "data", dataTypeName: "integer", hidden: false, key: false },
    { table: "DimEmployee", name: "EmployeeKey", kind: "data", dataTypeName: "integer", hidden: false, key: false },
    { table: "Date", name: "Date", kind: "calculatedTableColumn", dataTypeName: "datetime", hidden: false, key: true },
    { table: "Date", name: "Year", kind: "calculatedTableColumn", dataTypeName: "integer", hidden: false, key: false },
    { table: "Date", name: "Quarter", kind: "calculatedTableColumn", dataTypeName: "text", hidden: false, key: false },
  ],
  measures: [{ table: "# Measures", name: "Total Sales", formatString: "#,0", hidden: false }],
  relationships: [
    { fromTable: "FactSales", fromColumn: "StoreKey", toTable: "DimStore", toColumn: "StoreKey", cardinality: "M:1", crossFilter: "single", active: true },
    { fromTable: "DimStore", fromColumn: "Month Number", toTable: "FactSales", toColumn: "DateKey", cardinality: "1:M", crossFilter: "both", active: false },
  ],
  hierarchies: [{ table: "Date", name: "Calendar", hidden: false, levels: [{ name: "Quarter", column: "Quarter", ordinal: 1 }, { name: "Year", column: "Year", ordinal: 0 }] }],
  roles: [{ name: "Managers", permission: "read", tables: [{ table: "DimStore", filter: "[Status] = \"Open\"" }, { table: "FactSales", filter: "x".repeat(10), truncated: true }] }],
  calculationGroups: [{ table: "Time Calc", precedence: 10, items: [{ name: "PY", expression: "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))", ordinal: 1 }, { name: "Current", expression: "SELECTEDMEASURE()", ordinal: 0 }] }],
  fieldParameters: [{ table: "Sales Metric", labelColumn: "Sales Metric", rows: [{ label: "Avg", table: "# Measures", name: "Total Sales", order: 0, kind: "measure" }] }, { table: "Broken", rows: null }],
  partitions: [
    { table: "FactSales", name: "FactSales", source: "m", mode: "import", expression: "let\n  Source = Csv.Document(File.Contents(\"C:/data/sales.csv\"))\nin\n  Source" },
    { table: "DimStore", name: "DimStore", source: "m", expression: "let Source = 1", truncated: true },
  ],
  expressions: [{ name: "Region", kind: "m", expression: "\"East\" meta [IsParameterQuery=true, Type=\"Text\"]", parameter: true }, { name: "Base", kind: "m", expression: "let a = 1 in a", parameter: false }],
  studio: { rowLevelSecurity: "Studio's canvas and answers use every row.", powerBiOnly: [{ table: "Time Calc", kind: "calculationGroup", detail: "Visuals that use the calculation group Time Calc show in Power BI only." }], changedCalculations: [], data: { known: true, stale: false, tables: [], detail: "" }, actions: [] },
};
const facts = lib.readModelFacts(PAYLOAD);
const REPORT = {
  pages: [{ title: "Overview", visuals: [{ title: "Sales by store", bindings: [{ kind: "column", table: "DimStore", name: "StoreName", role: "Category" }, { kind: "measure", table: "# Measures", name: "Total Sales", role: "Y" }] }] }],
  measures: [{ TableName: "# Measures", Name: "Total Sales", Expression: "SUM(FactSales[SalesAmount])" }, { TableName: "DimStore", Name: "Open stores", Expression: "CALCULATE(COUNTROWS(DimStore), DimStore[Status] = \"Open\")" }],
};
const UNKNOWN = lib.NO_COVERAGE;
const ctxOf = (extra = {}) => ({ workspaceId: "ws1", facts, report: REPORT, coverage: UNKNOWN, locked: false, queue: () => {}, check: async () => ({ columns: [], rows: [], row_count: 0, truncated: false, elapsed_ms: 1, plan: "x" }), ...extra });

// --- reading the facts -------------------------------------------------------------------------------------------------

test("the payload's model reads into typed facts; sections it lacks are empty and a bare answer is unknown", () => {
  assert.equal(facts.status, "current");
  assert.deepEqual(facts.truncated, ["partitions"]);
  assert.equal(facts.settings.culture, "en-US");
  assert.equal(facts.tables.find(t => t.name === "Date").isDateTable, true);
  assert.equal(facts.columns.find(c => c.name === "Margin").kind, "calculated");
  assert.equal(facts.columns.find(c => c.name === "SalesAmount").dataType, "double");
  // Levels come in their ordinal order, whatever order the facts list them in.
  assert.deepEqual(facts.hierarchies[0].levels.map(l => l.column), ["Year", "Quarter"]);
  assert.equal(facts.roles[0].tables[1].truncated, true);
  assert.equal(facts.fieldParameters[1].rows, null);
  assert.equal(facts.studio.powerBiOnly.length, 1);
  assert.equal(lib.readModelFacts(undefined).status, "unknown");
  assert.equal(lib.readModelFacts({ status: "reading" }).status, "reading");
  assert.deepEqual(lib.readModelFacts({ status: "none" }).tables, []);
});

test("each model status has its plain note, and a current model has none", () => {
  assert.equal(lib.modelStatusNote({ status: "current" }), undefined);
  assert.match(lib.modelStatusNote({ status: "reading" }).text, /reading this report's model/);
  assert.equal(lib.modelStatusNote({ status: "unknown" }).tone, "warn");
  assert.match(lib.modelStatusNote({ status: "none" }).text, /no local model/);
});

test("table kinds follow Power BI's words and the automatic date tables are left out of the list", () => {
  const kind = name => lib.tableKind(facts, facts.tables.find(t => t.name === name));
  assert.equal(kind("FactSales"), "table");
  assert.equal(kind("Date"), "calculated table");
  assert.equal(kind("Time Calc"), "calculation group");
  assert.equal(kind("Sales Metric"), "field parameter");
  assert.ok(!lib.visibleTables(facts).some(t => t.name === "LocalDateTable_1"));
});

test("a table's Power Query is editable only when Studio has the whole text", () => {
  assert.equal(lib.tableQuery(facts, "FactSales").editable, true);
  const cut = lib.tableQuery(facts, "DimStore");
  assert.equal(cut.editable, false);
  assert.match(cut.reason, /cannot be changed here/);
  assert.equal(lib.tableQuery(facts, "Date"), undefined);
  assert.equal(lib.needsFullFacts(facts), true);
});

// --- operations ---------------------------------------------------------------------------------------------------------

test("column properties send only what changed, in the operation's words", () => {
  const store = facts.columns.find(c => c.name === "StoreName");
  const draft = lib.columnDraft(store);
  assert.equal(draft.dataCategory, "web_url");
  assert.equal(lib.columnPropertiesOp(store, draft), undefined, "an unchanged draft is no change");
  assert.deepEqual(lib.columnPropertiesOp(store, { ...draft, hidden: true, summarizeBy: "count", dataCategory: "image_url" }),
    { op: "set_column_properties", table: "DimStore", name: "StoreName", data_category: "image_url", summarize_by: "count", hidden: true });
  const month = facts.columns.find(c => c.name === "Month");
  assert.deepEqual(lib.columnPropertiesOp(month, { ...lib.columnDraft(month), sortBy: "" }), { op: "set_column_properties", table: "DimStore", name: "Month", sort_by: "" });
  // A data column never sends a data type or a formula; a calculated one does.
  const amount = facts.columns.find(c => c.name === "SalesAmount");
  assert.equal(lib.columnPropertiesOp(amount, { ...lib.columnDraft(amount), dataType: "fixed_decimal" }), undefined);
  const margin = facts.columns.find(c => c.name === "Margin");
  assert.deepEqual(lib.columnPropertiesOp(margin, { ...lib.columnDraft(margin), dataType: "fixed_decimal", expression: " FactSales[SalesAmount] * 0.25 " }),
    { op: "set_column_properties", table: "FactSales", name: "Margin", data_type: "fixed_decimal", expression: "FactSales[SalesAmount] * 0.25" });
  assert.equal(lib.summarizeValue("distinctCount"), "distinct_count");
  assert.equal(lib.dataTypeValue("integer"), "whole_number");
  assert.equal(lib.dataCategoryValue("StateOrProvince"), "state_or_province");
  assert.equal(lib.dataCategoryValue("Organization"), undefined);
});

test("table properties, renames, calculated columns and tables build the documented operations", () => {
  const date = facts.tables.find(t => t.name === "Date");
  assert.equal(lib.tablePropertiesOp(date, lib.tableDraft(date)), undefined);
  assert.deepEqual(lib.tablePropertiesOp(date, { ...lib.tableDraft(date), hidden: true, expression: "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))" }),
    { op: "set_table_properties", table: "Date", hidden: true, expression: "CALENDAR(DATE(2020,1,1), DATE(2025,12,31))" });
  assert.deepEqual(lib.renameColumnOp("DimStore", "StoreName", " Store "), { op: "rename_column", table: "DimStore", name: "StoreName", new_name: "Store" });
  assert.deepEqual(lib.calculatedColumnOp({ table: "FactSales", name: "Net", expression: "FactSales[SalesAmount]", formatString: "#,0.00", summarizeBy: "default" }),
    { op: "create_calculated_column", table: "FactSales", name: "Net", expression: "FactSales[SalesAmount]", format_string: "#,0.00" });
  assert.deepEqual(lib.calculatedTableOp({ name: "Years", expression: "EVALUATE DISTINCT('Date'[Year])" }), { op: "create_calculated_table", name: "Years", expression: "DISTINCT('Date'[Year])" });
  assert.equal(lib.isDefineQuery("DEFINE VAR x = 1 EVALUATE {x}"), true);
});

test("the DAX Lab check runs a calculated column on its table's rows and a table as it is", () => {
  assert.equal(lib.checkQuery("column", "FactSales[SalesAmount] * 2", "FactSales", "Double"), "EVALUATE ADDCOLUMNS(FactSales, \"Double\", FactSales[SalesAmount] * 2)");
  assert.equal(lib.checkQuery("column", "1", "# Measures", "A \"quoted\" name"), "EVALUATE ADDCOLUMNS('# Measures', \"A \"\"quoted\"\" name\", 1)");
  assert.equal(lib.checkQuery("table", "EVALUATE VALUES('Date'[Year])"), "EVALUATE VALUES('Date'[Year])");
  assert.equal(lib.checkQuery("table", "  "), undefined);
  assert.equal(lib.checkQuery("table", "x".repeat(4000)), undefined, "the lab takes at most 4,000 characters");
});

test("names are checked against the model: empty, too long, control characters and taken names", () => {
  const taken = lib.columnNamesTaken(facts, "DimStore");
  assert.ok(taken.includes("Total Sales"), "measures share the column namespace");
  assert.match(lib.modelNameProblem("", taken, "column"), /Give the column a name/);
  assert.match(lib.modelNameProblem("statusstatus".repeat(20), taken, "column"), /128/);
  assert.match(lib.modelNameProblem("a\tb", taken, "column"), /line breaks or tabs/);
  assert.match(lib.modelNameProblem("status", taken, "column"), /already used/);
  assert.equal(lib.modelNameProblem("Status", taken, "column", "Status"), undefined, "renaming to itself (another case) is fine");
  assert.equal(lib.modelNameProblem("Region", taken, "column"), undefined);
});

test("relationships are named many side first, whichever way the facts store them", () => {
  const flipped = lib.relationshipDraft(facts.relationships[1]);
  assert.deepEqual([flipped.fromTable, flipped.fromColumn, flipped.toTable, flipped.toColumn], ["FactSales", "DateKey", "DimStore", "Month Number"]);
  assert.equal(flipped.crossFilter, "both");
  const original = facts.relationships[0];
  assert.equal(lib.updateRelationshipOp(original, lib.relationshipDraft(original)), undefined);
  assert.deepEqual(lib.updateRelationshipOp(original, { ...lib.relationshipDraft(original), crossFilter: "both", active: false }),
    { op: "update_relationship", from_table: "FactSales", from_column: "StoreKey", to_table: "DimStore", to_column: "StoreKey", cross_filter: "both", active: false });
  assert.deepEqual(lib.deleteRelationshipOp(facts.relationships[1]), { op: "delete_relationship", from_table: "FactSales", from_column: "DateKey", to_table: "DimStore", to_column: "Month Number" });
  assert.equal(lib.relationshipKey(lib.relationshipDraft(original)), lib.relationshipKey({ fromTable: "dimstore", fromColumn: "storekey", toTable: "FactSales", toColumn: "StoreKey" }));
});

test("a new relationship is refused early when Power BI would refuse it, and warned about when it is risky", () => {
  const base = { fromTable: "FactSales", fromColumn: "StoreKey", toTable: "DimStore", toColumn: "StoreKey", cardinality: "many_to_one", crossFilter: "single", active: true };
  assert.match(lib.relationshipProblems(facts, base).blocking.join(" "), /already related/);
  assert.match(lib.relationshipProblems(facts, { ...base, toTable: "FactSales", toColumn: "DateKey" }).blocking.join(" "), /same table/);
  // A second active path between FactSales and DimStore.
  const second = { ...base, fromColumn: "SalesAmount", toColumn: "Status" };
  assert.match(lib.relationshipProblems(facts, second).blocking.join(" "), /one active path/);
  assert.deepEqual(lib.relationshipProblems(facts, { ...second, active: false }).blocking, []);
  const risky = lib.relationshipProblems(facts, { ...base, fromTable: "DimEmployee", fromColumn: "EmployeeKey", toColumn: "StoreKey", toTable: "DimStore", cardinality: "many_to_many", crossFilter: "both" });
  assert.deepEqual(risky.blocking, []);
  assert.equal(risky.warnings.length, 2);
  assert.deepEqual(lib.createRelationshipOp({ ...base, fromTable: "DimEmployee", fromColumn: "EmployeeKey" }),
    { op: "create_relationship", from_table: "DimEmployee", from_column: "EmployeeKey", to_table: "DimStore", to_column: "StoreKey", cardinality: "many_to_one", cross_filter: "single", active: true });
});

test("hierarchy levels reorder by drag or arrows and only a real change is sent", () => {
  assert.deepEqual(lib.moveItem(["a", "b", "c"], 0, 2), ["b", "c", "a"]);
  assert.deepEqual(lib.moveItem(["a", "b", "c"], 2, 0), ["c", "a", "b"]);
  assert.deepEqual(lib.moveItem(["a"], 3, 0), ["a"]);
  const calendar = facts.hierarchies[0];
  const levels = calendar.levels.map(l => ({ column: l.column, name: l.name }));
  assert.equal(lib.updateHierarchyOp(calendar, { name: "Calendar", levels }), undefined);
  assert.deepEqual(lib.updateHierarchyOp(calendar, { name: "Calendar", levels: lib.moveItem(levels, 1, 0) }),
    { op: "update_hierarchy", table: "Date", name: "Calendar", levels: [{ column: "Quarter" }, { column: "Year" }] });
  assert.deepEqual(lib.createHierarchyOp("Date", "Fiscal", [{ column: "Year", name: "FY" }]), { op: "create_hierarchy", table: "Date", name: "Fiscal", levels: [{ column: "Year", name: "FY" }] });
});

test("field parameters, calculation items and roles send what the user picked or changed", () => {
  const fields = lib.parameterFields(facts, facts.fieldParameters[0]);
  assert.deepEqual(fields, [{ table: "# Measures", name: "Total Sales", label: "Avg", kind: "measure" }]);
  assert.deepEqual(lib.fieldParameterOp("Sales Metric", [...fields, { table: "DimStore", name: "StoreName", label: "StoreName", kind: "column" }], true),
    { op: "update_field_parameter", name: "Sales Metric", fields: [{ table: "# Measures", name: "Total Sales", label: "Avg" }, { table: "DimStore", name: "StoreName" }] });
  const py = facts.calculationGroups[0].items[0];
  assert.equal(lib.calculationItemOp("Time Calc", py, lib.calculationItemDraft(py)), undefined);
  assert.deepEqual(lib.calculationItemOp("Time Calc", py, { ...lib.calculationItemDraft(py), ordinal: "5", formatStringExpression: "\"0%\"" }),
    { op: "set_calculation_item", group: "Time Calc", name: "PY", format_string_expression: "\"0%\"", ordinal: 5 });
  assert.deepEqual(lib.calculationItemOp("Time Calc", undefined, { name: "YTD", expression: "TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])", ordinal: "", formatStringExpression: "" }),
    { op: "set_calculation_item", group: "Time Calc", name: "YTD", expression: "TOTALYTD(SELECTEDMEASURE(), 'Date'[Date])" });
  assert.match(lib.ordinalProblem("-1"), /0 to 1000/);
  const role = facts.roles[0];
  // The truncated FactSales filter is kept as it is (never sent); the removed DimStore filter is sent as "".
  const kept = role.tables.filter(t => t.table === "FactSales").map(t => ({ table: t.table, expression: t.filter }));
  assert.deepEqual(lib.roleOp(role, { name: "Managers", description: "", filters: kept }), { op: "update_role", name: "Managers", filters: [{ table: "DimStore", expression: "" }] });
  assert.deepEqual(lib.roleOp(undefined, { name: " East ", description: "", filters: [{ table: "DimStore", expression: "[Region] = \"East\"" }, { table: "FactSales", expression: " " }] }),
    { op: "create_role", name: "East", filters: [{ table: "DimStore", expression: "[Region] = \"East\"" }] });
});

test("Power Query: the obvious slips are pointed at before queueing, and the operation carries what to do if the reload fails", () => {
  assert.deepEqual(lib.mQueryProblems("let\n  Source = Csv.Document(File.Contents(\"C:/a.csv\")) // (\nin\n  Source"), []);
  assert.deepEqual(lib.mQueryProblems("let #\"Step (1\" = 1 in #\"Step (1\""), [], "quoted names may hold brackets");
  assert.match(lib.mQueryProblems("let Source = 1").join(" "), /matching “in”/);
  assert.match(lib.mQueryProblems("let Source = Table.FromRows({ {1, 2} in Source").join(" "), /brackets/);
  assert.match(lib.mQueryProblems("let Source = \"open in Source").join(" "), /not closed/);
  assert.deepEqual(lib.mQueryProblems(""), ["The query is empty."]);
  assert.deepEqual(lib.tableQueryOp("FactSales", "let a = 1\r\nin a\n"), { op: "set_table_query", table: "FactSales", expression: "let a = 1\nin a" });
  assert.deepEqual(lib.tableQueryOp("FactSales", "let a = 1 in a", "keep"), { op: "set_table_query", table: "FactSales", expression: "let a = 1 in a", if_refresh_fails: "keep" });
  assert.match(lib.REFRESH_WARNING, /cannot be reached/);
  assert.equal(lib.parameterValue("\"East\" meta [IsParameterQuery=true]"), "East");
  assert.equal(lib.parameterValue("42 meta [IsParameterQuery=true]"), "42");
  assert.deepEqual(lib.modelOptionsOp(facts.settings, { discourageImplicitMeasures: true }), { op: "set_model_options", discourage_implicit_measures: true });
  assert.equal(lib.modelOptionsOp(facts.settings, { discourageImplicitMeasures: false }), undefined);
});

// --- guards ---------------------------------------------------------------------------------------------------------------

test("a column delete names every relationship, sort, hierarchy, visual and formula that still uses it", () => {
  const key = lib.columnDeleteGuard(facts, REPORT, "DimStore", "StoreKey");
  assert.match(key.blocking[0], /relationship/);
  const sorted = lib.columnDeleteGuard(facts, REPORT, "DimStore", "Month Number");
  assert.ok(sorted.blocking.some(line => /Month is sorted by it/.test(line)));
  assert.ok(lib.columnDeleteGuard(facts, REPORT, "Date", "Year").blocking.some(line => /hierarchy Calendar/.test(line)));
  assert.ok(lib.columnDeleteGuard(facts, REPORT, "DimStore", "StoreName").blocking.some(line => /Sales by store/.test(line)));
  const status = lib.columnDeleteGuard(facts, REPORT, "DimStore", "Status");
  assert.ok(status.blocking.some(line => /Open stores/.test(line) && /role Managers/.test(line)), status.blocking.join(" | "));
  const amount = lib.columnDeleteGuard(facts, REPORT, "FactSales", "SalesAmount");
  assert.ok(amount.blocking.some(line => /Total Sales/.test(line) && /FactSales\[Margin\]/.test(line)));
  assert.deepEqual(lib.columnDeleteGuard(facts, REPORT, "DimEmployee", "EmployeeKey"), { blocking: [], warnings: [] });
});

test("a table delete lists what uses it and warns that its measures go with it", () => {
  const store = lib.tableDeleteGuard(facts, REPORT, "DimStore");
  assert.ok(store.blocking.some(line => /relationships/.test(line)));
  assert.ok(store.blocking.some(line => /Sales by store/.test(line)));
  assert.ok(store.warnings.some(line => /Open stores/.test(line)));
  assert.ok(store.warnings.some(line => /Managers/.test(line)));
  assert.deepEqual(lib.tableDeleteGuard(facts, REPORT, "DimEmployee").blocking, []);
});

// --- Microsoft's supported list ------------------------------------------------------------------------------------------

test("changes outside Microsoft's list are found as the server finds them", () => {
  assert.equal(lib.supportKey({ op: "set_column_properties", table: "DimStore", name: "Status", hidden: true }, facts), undefined);
  assert.equal(lib.supportKey({ op: "rename_column", table: "DimStore", name: "Status", new_name: "State" }, facts), "rename_column");
  assert.equal(lib.supportKey({ op: "update_hierarchy", table: "Date", name: "Calendar" }, facts), "hierarchies");
  assert.equal(lib.supportKey({ op: "delete_column", table: "DimStore", name: "Status" }, facts), "delete_data_column");
  assert.equal(lib.supportKey({ op: "delete_column", table: "FactSales", name: "Margin" }, facts), undefined, "a calculated column is on the list");
  assert.equal(lib.supportKey({ op: "set_table_properties", table: "FactSales", hidden: true }, facts), "data_table_properties");
  assert.equal(lib.supportKey({ op: "set_table_properties", table: "Date", hidden: true }, facts), undefined);
  assert.equal(lib.supportKey({ op: "delete_table", name: "Time Calc" }, facts), undefined);
  assert.equal(lib.supportKey({ op: "set_table_query", table: "FactSales", expression: "x" }, facts), "set_table_query");
});

// capabilities.model_support as the payload carries it (docs/imported-report-editing-api.md).
const support = (verifiedKeys, version = "2.158.1177.0") => {
  const keys = { rename_column: "rename_column", rename_table: "rename_table", mark_date_table: "mark_date_table", create_hierarchy: "hierarchies",
    update_hierarchy: "hierarchies", delete_hierarchy: "hierarchies", set_table_query: "set_table_query", refresh_data: "refresh_data",
    delete_column: "delete_data_column", delete_table: "delete_data_table", set_table_properties: "data_table_properties" };
  const depends = ["delete_column", "set_table_properties", "delete_table"];
  const outside = Object.keys(keys).filter(op => !depends.includes(op));
  return { model_support: { desktop_version: version, documented: ["create_calculated_column", "create_role"], outside, depends, keys,
    verified_keys: verifiedKeys, verified: Object.keys(keys).filter(op => verifiedKeys.includes(keys[op])) } };
};

test("capabilities.model_support decides: a verified outside change says so, an unverified one is disabled with Desktop's steps", () => {
  const rename = { op: "rename_table", name: "DimStore", new_name: "Stores" };
  const coverage = lib.readSupportCoverage(support(["rename_table", "hierarchies"]));
  assert.equal(coverage.known, true);
  assert.equal(coverage.keys.create_hierarchy, "hierarchies");
  const tested = lib.supportState(rename, facts, coverage);
  assert.equal(tested.blocked, false);
  assert.equal(tested.label, "Outside Microsoft's supported list; tested with Power BI Desktop 2.158.1177.0");
  assert.equal(lib.supportState({ op: "update_hierarchy", table: "Date", name: "Calendar" }, facts, coverage).blocked, false, "one key covers every hierarchy operation");
  const untested = lib.supportState({ op: "rename_column", table: "DimStore", name: "Status", new_name: "State" }, facts, coverage);
  assert.equal(untested.blocked, true);
  assert.match(untested.steps, /right-click the column/);
  assert.match(untested.label, /not tested with Power BI Desktop 2.158.1177.0/);
  // Without the payload's word nothing is disabled here; the server still refuses on Apply.
  const unknown = lib.supportState(rename, facts, lib.readSupportCoverage({ edit_model: true }, "2.158.1177.0"));
  assert.equal(unknown.blocked, false);
  assert.equal(unknown.outside, true);
  assert.deepEqual(lib.supportState({ op: "create_role", name: "x" }, facts, coverage), { outside: false, blocked: false });
  assert.equal(lib.combinedSupport([{ op: "create_role", name: "x" }, { op: "refresh_data" }], facts, coverage).blocked, true);
  // No Desktop found: nothing outside the list can be sent.
  assert.equal(lib.supportState(rename, facts, lib.readSupportCoverage(support([], null))).blocked, true);
});

test("a depends operation that is not verified works on calculated objects only", () => {
  const coverage = lib.readSupportCoverage(support([]));
  assert.equal(lib.supportState({ op: "delete_column", table: "FactSales", name: "Margin" }, facts, coverage).outside, false);
  assert.equal(lib.supportState({ op: "set_table_properties", table: "Date", hidden: true }, facts, coverage).blocked, false);
  assert.equal(lib.supportState({ op: "delete_table", name: "Time Calc" }, facts, coverage).blocked, false);
  const data = lib.supportState({ op: "delete_column", table: "DimStore", name: "Status" }, facts, coverage);
  assert.equal(data.blocked, true);
  assert.match(data.steps, /Delete from model/);
  assert.equal(lib.supportState({ op: "set_table_properties", table: "FactSales", hidden: true }, facts, coverage).blocked, true);
  assert.equal(lib.supportState({ op: "set_table_properties", table: "FactSales", hidden: true }, facts, lib.readSupportCoverage(support(["data_table_properties"]))).blocked, false);
});

// --- the tray -------------------------------------------------------------------------------------------------------------

test("model changes read as plain lines in the tray; Power Query is marked as M", () => {
  assert.equal(lib.describeModelOp({ op: "set_column_properties", table: "DimStore", name: "Month", sort_by: "Month Number", hidden: true }), "Column DimStore[Month]: sort by Month Number, hide");
  assert.equal(lib.describeModelOp({ op: "mark_date_table", table: "Date", column: null }), "Unmark Date as a date table");
  assert.equal(lib.describeModelOp({ op: "refresh_data" }), "Refresh all the data");
  assert.equal(lib.describeModelOp({ op: "update_measure", name: "x" }), undefined, "measure changes keep the tray's own words");
  assert.equal(lib.expressionLanguage({ op: "set_table_query", expression: "let a = 1 in a" }), "m");
  assert.equal(lib.expressionLanguage({ op: "create_calculated_column", expression: "1" }), "dax");
  assert.equal(lib.isModelFamilyOperation({ op: "create_hierarchy" }), true);
  assert.equal(lib.isModelFamilyOperation({ op: "create_measure" }), false);
});

test("a second change to the same object's settings folds into the first; other changes queue in order", () => {
  let n = 0;
  const store = lib.createPendingStore(() => `id${++n}`);
  assert.equal(lib.queueModelChange(store, "ws", { op: "set_column_properties", table: "DimStore", name: "Month", hidden: true }), "added");
  assert.equal(lib.queueModelChange(store, "ws", { op: "create_calculated_column", table: "FactSales", name: "Net", expression: "1" }), "added");
  assert.equal(lib.queueModelChange(store, "ws", { op: "set_column_properties", table: "dimstore", name: "month", sort_by: "Month Number" }), "merged");
  const changes = store.get("ws");
  assert.equal(changes.length, 2);
  assert.deepEqual(changes[1].op, { op: "set_column_properties", table: "dimstore", name: "month", hidden: true, sort_by: "Month Number" });
  // New calculation items and renames never fold.
  lib.queueModelChange(store, "ws", { op: "set_calculation_item", group: "Time Calc", name: "PY", ordinal: 3 });
  lib.queueModelChange(store, "ws", { op: "set_calculation_item", group: "Time Calc", name: "PY", ordinal: 4 });
  lib.queueModelChange(store, "ws", { op: "rename_column", table: "DimStore", name: "Status", new_name: "A" });
  lib.queueModelChange(store, "ws", { op: "rename_column", table: "DimStore", name: "Status", new_name: "B" });
  assert.equal(store.get("ws").length, 5);
  assert.equal(store.get("ws").filter(c => c.op.op === "set_calculation_item")[0].op.ordinal, 4);
});

test("the tray names model changes, labels Power Query as M, and disables Apply for a change Studio does not make with this Desktop", () => {
  const ws = "tray-ws";
  lib.pendingStore.add(ws, { op: "set_table_query", table: "FactSales", expression: "let a = 1 in a" });
  lib.pendingStore.add(ws, { op: "create_calculated_column", table: "FactSales", name: "Net", expression: "FactSales[SalesAmount]" });
  const coverage = lib.readSupportCoverage(support([]));
  const html = render(lib.PendingModelChanges, { workspaceId: ws, disabled: false, onApply: () => {}, onDax: () => {}, facts, coverage, capabilities: { edit_report: true, edit_model: true, reasons: [], thin_report: false } });
  assert.match(html, /Change the Power Query of FactSales and reload it/);
  assert.match(html, /aria-label="Power Query \(M\)"/);
  assert.match(html, /aria-label="DAX"/);
  assert.match(html, /not tested with Power BI Desktop 2.158.1177.0/);
  assert.match(html, /Home &gt; Transform data/);
  assert.match(html, /<button type="button" class="primary yellow full" disabled="">Apply to report/);
  // "Try it in DAX Lab first" is offered for DAX only.
  assert.equal((html.match(/Try it in DAX Lab first/g) ?? []).length, 1);
  lib.pendingStore.clear(ws);
});

test("more than fifty pending changes say that the first fifty are applied now", () => {
  const ws = "many";
  for (let i = 0; i < 52; i++) lib.pendingStore.add(ws, { op: "create_calculated_column", table: "FactSales", name: `C${i}`, expression: "1" });
  const html = render(lib.PendingModelChanges, { workspaceId: ws, disabled: false, onApply: () => {} });
  assert.match(html, /at most 50 changes at a time/);
  lib.pendingStore.clear(ws);
});

// --- model health ---------------------------------------------------------------------------------------------------------

const HEALTH = {
  known: true, head: { id: "r0003", hash: "h3" }, checked: { tables: 9 }, notes: ["Column statistics were not used."],
  summary: { problems: 1, suggestions: 2, by_category: { correctness: 1, performance: 1, usability: 1, formatting: 0, naming: 0 }, by_rule: {} },
  findings: [
    { id: "unused_column:DimStore", rule: "unused_column", category: "performance", severity: "suggestion", objects: [{ kind: "column", table: "DimStore", name: "Status" }],
      message: "DimStore has a column used nowhere: Status.", why: "Unused columns make the file bigger.", fix: [{ op: "set_column_properties", table: "DimStore", name: "Status", hidden: true }],
      fix_summary: "Hide the column.", fix_needs_desktop: true, fix_confirm: true, fix_outside_supported: false },
    { id: "broken_object:x", rule: "broken_object", category: "correctness", severity: "problem", objects: [{ kind: "measure", table: "# Measures", name: "Bad" }],
      message: "[Bad] has an error.", why: "It shows no number.", fix: null, fix_summary: "Open the measure and fix its formula.", fix_needs_desktop: true, fix_outside_supported: false },
    { id: "two_way_relationship:a", rule: "two_way_relationship", category: "usability", severity: "suggestion",
      objects: [{ kind: "relationship", table: "FactSales", name: "DateKey", to_table: "DimStore", to_column: "Month Number" }], message: "Filters both ways.", why: "Ambiguous.",
      fix: [{ op: "rename_table", name: "DimStore", new_name: "Stores" }], fix_summary: "Rename it.", fix_needs_desktop: true, fix_outside_supported: true },
    { id: "nope", category: "weird", message: "dropped" },
  ],
};

test("health findings read, group by category in the fixed order, and name their objects plainly", () => {
  const health = lib.readHealth(HEALTH);
  assert.equal(health.findings.length, 3, "a finding of an unknown category is left out");
  assert.equal(health.head.hash, "h3");
  assert.deepEqual(lib.groupFindings(health.findings).map(g => g.category), ["correctness", "performance", "usability"]);
  assert.deepEqual(lib.groupFindings(health.findings, { severity: "problem" }).map(g => g.findings.length), [1]);
  assert.deepEqual(lib.groupFindings(health.findings, { category: "usability" }).map(g => g.label), ["Usability"]);
  assert.equal(lib.objectLabel(health.findings[2].objects[0]), "FactSales[DateKey] → DimStore[Month Number]");
  assert.equal(lib.objectLabel({ kind: "model" }), "The model");
  assert.deepEqual(lib.healthFix(health.findings[1]), { byHand: "Open the measure and fix its formula." });
  assert.equal(lib.healthFix(health.findings[0]).ops.length, 1);
  assert.equal(lib.readHealth({ known: false }).known, false);
});

test("the health section starts by checking the model", () => {
  const html = render(lib.ModelEditorHealth, { ctx: ctxOf(), headId: "r1", load: async () => lib.readHealth(HEALTH), onFocusObject: () => {} });
  assert.match(html, /Model health/);
  assert.match(html, /aria-busy="true"/);
  assert.match(html, /Use data statistics/);
});

// --- the routes -----------------------------------------------------------------------------------------------------------

test("the model client reads the full facts, the health check and re-reads the data through the documented routes", async () => {
  const calls = [];
  const answers = [
    { status: 200, body: { ...PAYLOAD, status: "current" } },
    { status: 404, body: { detail: { code: "unknown_table", message: "The model has no table 'Nope'." } } },
    { status: 200, body: HEALTH },
    { status: 202, body: { batch_id: "b1", state: "queued" } },
    { status: 200, body: { batch_id: "b1", state: "running" } },
    { status: 200, body: { batch_id: "b1", state: "done" } },
  ];
  const transport = async (method, path, body) => { calls.push({ method, path, body }); return answers.shift(); };
  const client = lib.createModelClient(transport, { sleep: async () => {}, key: () => "key-12345678" });
  const full = await client.fullFacts("ws 1");
  assert.equal(full.tables.length, PAYLOAD.tables.length);
  await assert.rejects(client.fullFacts("ws 1", "Nope"), /no table 'Nope'/);
  const health = await client.health("ws 1", true);
  assert.equal(health.findings.length, 3);
  await client.rereadData("ws 1");
  assert.deepEqual(calls.map(c => `${c.method} ${c.path}`), [
    "GET /v1/workspaces/ws%201/imported-report/model",
    "GET /v1/workspaces/ws%201/imported-report/model?table=Nope",
    "GET /v1/workspaces/ws%201/imported-report/model-health?stats=true",
    "POST /v1/workspaces/ws%201/imported-report/reread-data",
    "GET /v1/workspaces/ws%201/imported-report/edits/b1",
    "GET /v1/workspaces/ws%201/imported-report/edits/b1",
  ]);
  assert.deepEqual(calls[3].body, { idempotency_key: "key-12345678" });
});

test("a Power Query formula is checked through validate-query, with the table when it is a table's query", async () => {
  const calls = [];
  const answers = [
    { status: 200, body: { valid: false, checked: true, message: "The Power Query formula is not valid: Expected 'in'", problems: ["Expected 'in'"], warnings: [], table: "FactSales" } },
    { status: 200, body: { valid: true, checked: false, message: null, problems: [], warnings: ["Power BI checks the query when it loads the data."], table: null } },
    { status: 404, body: { code: "no_model", message: "This report reads its data from a shared model." } },
  ];
  const client = lib.createModelClient(async (method, path, body) => { calls.push({ method, path, body }); return answers.shift(); });
  const refused = await client.validateQuery("ws", "let a = 1", "factsales");
  assert.deepEqual(calls[0], { method: "POST", path: "/v1/workspaces/ws/imported-report/model/validate-query", body: { expression: "let a = 1", table: "factsales" } });
  assert.equal(refused.valid, false);
  assert.equal(refused.table, "FactSales");
  // The refusal is shown once, not again as its own problem.
  assert.deepEqual(lib.queryCheckView(refused), { tone: "error", lines: ["The Power Query formula is not valid: Expected 'in'"] });
  const unchecked = await client.validateQuery("ws", "let a = 1 in a");
  assert.deepEqual(calls[1].body, { expression: "let a = 1 in a" });
  assert.deepEqual(lib.queryCheckView(unchecked), { tone: "warn", lines: ["Power BI checks the query when it loads the data."] });
  await assert.rejects(client.validateQuery("ws", "x"), /shared model/);
  assert.deepEqual(lib.queryCheckView(lib.readQueryCheck({ valid: true, checked: true, problems: [], warnings: [] })).tone, "ok");
  assert.deepEqual(lib.queryCheckView(lib.readQueryCheck({ valid: false, checked: true, problems: ["A", "B"] })).lines, ["Power BI would refuse this query.", "A", "B"]);
});

test("the Power Query editor offers Check when the server can validate queries", () => {
  const html = render(lib.ModelEditorQuery, { ctx: ctxOf({ validateQuery: async () => lib.readQueryCheck({ valid: true, checked: true }) }), full: "ready", onRetry: () => {} });
  assert.match(html, />Check<\/button>/);
  assert.match(html, /aria-live="polite"/);
  assert.doesNotMatch(render(lib.ModelEditorQuery, { ctx: ctxOf(), full: "ready", onRetry: () => {} }), />Check<\/button>/);
  assert.equal(lib.QUERY_CHECK_DELAY_MS, 800);
});

test("a failed re-read says why", async () => {
  const answers = [{ status: 202, body: { batch_id: "b2" } }, { status: 200, body: { state: "failed", error: { code: "x", message: "The data file is locked." } } }];
  const client = lib.createModelClient(async () => answers.shift(), { sleep: async () => {} });
  await assert.rejects(client.rereadData("ws"), /locked/);
});

// --- the sections render ------------------------------------------------------------------------------------------------------

test("tables and columns: the list, the kinds, the columns of the selected table and the column editor", () => {
  const html = render(lib.ModelEditorTables, { ctx: ctxOf(), selected: { table: "DimStore", column: "Month" }, onSelect: () => {}, rowCounts: { DimStore: 12 } });
  assert.match(html, /aria-label="Tables"/);
  assert.doesNotMatch(html, /LocalDateTable_1/);
  assert.match(html, /calculation group/);
  assert.match(html, /12 rows in the local snapshot/);
  assert.match(html, /does not establish complete native model coverage/);
  assert.match(html, /aria-label="Column Month"/);
  assert.match(html, /<option value="Month Number" selected="">Month Number<\/option>/);
  assert.match(html, /Set in Power Query|set in Power Query/);
});

test("relationships: the diagram-lite draws one keyboard-reachable line per relationship, dashed when inactive", () => {
  const html = render(lib.ModelEditorRelationships, { ctx: ctxOf(), onSelect: () => {} });
  assert.equal((html.match(/class="me-edge[^"]*" role="button" tabindex="0"/g) ?? []).length, 2);
  assert.match(html, /class="me-edge inactive"/);
  assert.match(html, /Many to one \(\*:1\)/);
  const at = lib.diagramLayout(["A", "B", "C"]);
  assert.ok(Object.values(at).every(p => p.x > 0 && p.x < 720 && p.y > 0 && p.y < 300));
});

test("the other sections render with the model's objects and the outside-the-list badge where it applies", () => {
  assert.match(render(lib.ModelEditorHierarchies, { ctx: ctxOf() }), /Calendar/);
  assert.match(render(lib.ModelEditorFieldParameters, { ctx: ctxOf() }), /fields not read/);
  assert.match(render(lib.ModelEditorCalculationGroups, { ctx: ctxOf() }), /precedence 10/);
  assert.match(render(lib.ModelEditorRoles, { ctx: ctxOf() }), /row-level security/);
  const query = render(lib.ModelEditorQuery, { ctx: ctxOf(), full: "ready", onRetry: () => {} });
  assert.match(query, /Csv.Document/);
  assert.match(query, /Outside Microsoft&#x27;s supported list/);
  assert.match(render(lib.ModelEditorQuery, { ctx: ctxOf(), full: "loading", onRetry: () => {} }), /Reading the queries in full/);
  const settings = render(lib.ModelEditorSettings, { ctx: ctxOf({ coverage: lib.readSupportCoverage(support([], "2.158")) }) });
  assert.match(settings, /Discourage implicit measures/);
  assert.match(settings, /Options and settings/);
});

test("a delete that something still uses is shown with the reasons and cannot be queued", () => {
  const guard = lib.columnDeleteGuard(facts, REPORT, "DimStore", "StoreKey");
  const html = render(lib.DeleteConfirm, { ctx: ctxOf(), what: "the column DimStore[StoreKey]", guard, op: { op: "delete_column", table: "DimStore", name: "StoreKey" }, onDone: () => {} });
  assert.match(html, /refuses this delete/);
  assert.match(html, /disabled=""[^>]*>Queue the delete/);
});

test("DAX Lab offers to save a formula as a calculated column or table, checked before it is queued", () => {
  const html = render(lib.SaveImportedCalculation, { workspaceId: "ws", tables: ["FactSales", "DimStore"], columnsOf: () => ["SalesAmount"], measureNames: [], expression: "VALUES('Date'[Year])", isQuery: true, disabled: false, onCheck: async () => ({}) });
  assert.match(html, /Save as a calculated column or table/);
  assert.match(html, /checked="" name="dax-save-kind"|name="dax-save-kind" checked=""/);
  assert.match(html, /Check the formula/);
  assert.doesNotMatch(html, /Queue as a table/, "the queue button appears only after the check");
});
