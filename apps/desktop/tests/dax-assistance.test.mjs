import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("../src/daxAssistance.ts", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { daxAssistantPrompt, normalizeDaxSuggestion, numericResultColumns, daxChartRows, daxExamples, extractDaxFormula, daxResultDefaults, isDaxTableExpression } = loaded.exports;
const result = rows => ({ columns: ["Region", "Total"], rows, row_count: rows.length, truncated: false, elapsed_ms: 4, plan: "grouped" });

test("only complete unambiguous DAX answers can replace the editor", () => {
  assert.deepEqual(extractDaxFormula("```dax\nAverage Sales = AVERAGE('Orders'[Sales])\n```\nAverages the sales."), { expression: "AVERAGE('Orders'[Sales])", name: "Average Sales" });
  for (const answer of ["Use SUM", "```dax\nSUM('Orders'[Sales])", "```sql\nSELECT 1\n```", "```dax\n[Sales]\n```\n```dax\n[Profit]\n```", "```dax\n[Sales]\n```\n```dax\n[Profit]", `\`\`\`dax\n${"x".repeat(4001)}\n\`\`\``]) {
    assert.equal(extractDaxFormula(answer), undefined);
  }
});

test("numeric identifiers are categories, not default chart measures", () => {
  const data = { ...result([{ "Row ID": 1, "Average Sales": 10 }, { "Row ID": 2, "Average Sales": 20 }]), columns: ["Row ID", "Average Sales"] };
  assert.deepEqual(daxResultDefaults(data), { category: "Row ID", metric: "Average Sales", view: "table" });
  const code = { ...data, columns: ["Account", "Average Sales"], rows: [{ Account: 100, "Average Sales": 2 }] };
  assert.equal(daxResultDefaults(code, { tables: [{ table: { columns: [{ name: "Account", candidate_key: true }] } }] }).metric, "Average Sales");
  const grouped = result([{ Region: "West", Total: 0 }, { Region: "East", Total: -1 }]);
  assert.deepEqual(daxResultDefaults(grouped), { category: "Region", metric: "Total", view: "visual" });
  assert.equal(daxResultDefaults(grouped, { tables: [{ table: { columns: [{ name: "Region", candidate_key: true }] } }] }).view, "visual");
  assert.equal(daxResultDefaults({ ...grouped, truncated: true }).view, "table");
  assert.equal(daxResultDefaults(result(Array.from({ length: 21 }, (_, i) => ({ Region: String(i), Total: i })))).view, "table");
});

test("table expressions cannot be offered as scalar measures without EVALUATE", () => {
  for (const text of ['EVALUATE Orders', 'SUMMARIZECOLUMNS(Orders[Region], "Total", SUM(Orders[Sales]))', 'VALUES(Orders[Region])', 'TOPN(5, Orders)']) assert.equal(isDaxTableExpression(text), true);
  assert.equal(isDaxTableExpression('SUM(Orders[Sales])'), false);
});

test("DAX requests stay discussion-only and retain current formula and evaluation error", () => {
  const prompt = daxAssistantPrompt("Create sales by region", "SUM('Orders'[Sales])", "Unknown field");
  assert.match(prompt, /^Explain and suggest DAX only\. Do not change/);
  assert.match(prompt, /SUM\('Orders'\[Sales\]\)/);
  assert.match(prompt, /Unknown field/);
  assert.match(prompt, /My request: Create sales by region$/);
  assert.ok(prompt.length <= 4000);
  assert.throws(() => daxAssistantPrompt("Help", "x".repeat(4000)), /too long/);
});

test("measure assignments are separated without corrupting VAR or comparison expressions", () => {
  assert.deepEqual(normalizeDaxSuggestion("Profit Margin = DIVIDE([Profit], [Sales])"), { name: "Profit Margin", expression: "DIVIDE([Profit], [Sales])" });
  for (const expression of ["VAR total = SUM('Orders'[Sales])\nRETURN total", "[Sales] = 10", "'Orders'[Sales] = 10", 'CALCULATE([Sales], \'Orders\'[Region] = "West")']) {
    assert.deepEqual(normalizeDaxSuggestion(expression), { name: "", expression });
  }
});

test("charts preserve zero and negatives, omit blanks, and never coerce strings into numbers", () => {
  const data = result([{ Region: "A", Total: 0 }, { Region: "B", Total: -12 }, { Region: "C", Total: null }]);
  assert.deepEqual(numericResultColumns(data), ["Total"]);
  assert.deepEqual(daxChartRows(data, "Region", "Total"), [{ category: "A", value: 0 }, { category: "B", value: -12 }]);
  assert.deepEqual(numericResultColumns(result([{ Region: "A", Total: "12" }])), []);
  assert.deepEqual(numericResultColumns(result([{ Region: "A", Total: Infinity }])), []);
});

test("duplicate categories remain separate rows and previews are bounded", () => {
  const data = result(Array.from({ length: 30 }, (_, i) => ({ Region: "West", Total: i })));
  const rows = daxChartRows(data, "Region", "Total");
  assert.equal(rows.length, 20);
  assert.equal(new Set(rows.map(row => row.category)).size, 20);
  assert.equal(rows[0].value, 0);
  assert.equal(rows[19].value, 19);
});

test("multiple groups and grand totals open as a table and total is never a chart bar", () => {
  const data = { ...result([
    { Region: "East", Category: "Office", Total: 2, IsTotal: false },
    { Region: "West", Category: "Office", Total: 3, IsTotal: false },
    { Region: null, Category: null, Total: 5, IsTotal: true },
  ]), columns: ["Region", "Category", "Total", "IsTotal"], grand_total_column: "IsTotal" };
  assert.equal(daxResultDefaults(data).view, "table");
  assert.equal(daxResultDefaults(data).category, "Region");
  assert.deepEqual(daxChartRows(data, "Region", "Total"), [{ category: "East", value: 2 }, { category: "West", value: 3 }]);
  const noTotal = { ...data, columns: ["Region", "Category", "Total"], rows: data.rows.slice(0, 2), grand_total_column: undefined };
  assert.equal(daxResultDefaults(noTotal).view, "table");
});

test("examples use imported names instead of assuming the sample Orders schema", () => {
  const snapshot = { tables: [{ table: { name: "Manager's Orders", columns: [{ name: "Amount", data_type: "number" }, { name: "Area", data_type: "string", distinct_count: 4 }] } }] };
  const examples = daxExamples(snapshot);
  assert.equal(examples.find(item => item.label === "Total").expression, "SUM('Manager''s Orders'[Amount])");
  assert.match(examples.find(item => item.label === "Grouped").expression, /\[Area\]/);
  assert.deepEqual(daxExamples(undefined), []);
});


test("accidental joined schema references are caught without rewriting valid calculations", () => {
  const { joinedDaxReferences } = loaded.exports;
  assert.equal(joinedDaxReferences("'Date'[Date]SUM(FactSales[SalesAmount])[MTD Total Sales]"), true);
  assert.equal(joinedDaxReferences("SUM(FactSales[SalesAmount])[MTD Total Sales]"), true);
  for (const expression of ["[MTD Total Sales]", "SUM(FactSales[SalesAmount]) - [Discount]", "CALCULATE([Sales], 'Date'[Year] = 2025)"]) assert.equal(joinedDaxReferences(expression), false);
});
test("native breakdown quotes source names and keeps the original calculation inside filter context", () => {
  const { nativeBreakdownQuery } = loaded.exports;
  const query = nativeBreakdownQuery("[MTD Total Sales]", "Customer's table", "Name]suffix");
  assert.match(query, /'Customer''s table'\[Name\]\]suffix\]/);
  assert.match(query, /SUMMARIZECOLUMNS/);
  assert.match(query, /IGNORE\(ISNUMBER/);
  assert.throws(() => nativeBreakdownQuery("x".repeat(4000), "T", "C"), /too long/);
});

test("native grouping distinguishes repeated column IDs in different imported tables", () => {
  const { nativeGroupingFields, nativeBreakdownQuery } = loaded.exports;
  const fields = nativeGroupingFields([
    { id: "date-table", table: { name: "Date", columns: [{ id: "date", name: "Date" }] } },
    { id: "template-table", table: { name: "DateAutoTemplate", columns: [{ id: "date", name: "Date" }] } },
  ]);
  assert.notEqual(fields[0].id, fields[1].id);
  const selected = fields.find(field => field.id === fields[1].id);
  assert.match(nativeBreakdownQuery("[Sales]", selected.table, selected.name), /SUMMARIZECOLUMNS\('DateAutoTemplate'\[Date\]/);
});
