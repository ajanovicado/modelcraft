import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";
import { compile } from "vega-lite";
import * as vega from "vega";

// Round 3 of the render contract (docs/imported-report-render-contract.md sections 12 to 23): conditional
// formatting, number formats, totals, analytics lines, highlights, small multiples, buttons and actions,
// drillthrough, tooltips, synced slicers, bookmarks, the phone layout and the formatting-only merge.
const require = createRequire(import.meta.url);
async function load(file) {
  const path = new URL(`../src/${file}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  const compiled = await build({ entryPoints: [path], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", logLevel: "silent" });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(require, loaded, loaded.exports);
  return loaded.exports;
}
const r = await load("reportRender.ts");
const f = await load("reportFormat.ts");
const c = await load("reportCells.ts");
const a = await load("reportActions.ts");

const theme = { dataColors: ["#0B6E4F", "#F26419", "#33658A"] };
const column = (kind, name, role, extra = {}) => ({ kind, table: "Orders", name, label: name, role, query_ref: `Orders.${name}`, format: "", alignment: "", colorIndex: null, hidden: false, ...extra });
const chart = (extra = {}) => ({
  id: "chart", title: "Sales by Region", kind: "column", columns: [column("column", "Region", "Category"), column("measure", "Sales", "Y")],
  rows: [["West", 5], ["East", 12], ["North", 8]], formatted_rows: [[null, "5"], [null, "12"], [null, "8"]], selected: [], truncated: false,
  formatting: { chart: { stacking: "clustered" }, series: { categoryColors: [] }, labels: { show: false }, legend: { show: true }, axes: {} }, ...extra,
});

/** Compiles a built chart with Vega-Lite and draws it with Vega: warnings and the SVG. */
async function draw(built) {
  const warnings = [];
  const logger = { level() { return this; }, warn(...args) { warnings.push(args.join(" ")); return this; }, info() { return this; }, debug() { return this; }, error(...args) { warnings.push(`error ${args.join(" ")}`); return this; } };
  const out = compile({ ...built.spec, width: 300, height: 200, config: built.config }, { logger });
  const view = new vega.View(vega.parse(out.spec), { renderer: "none" });
  return { warnings, svg: await view.toSVG() };
}

// --- number formats (section 13) -------------------------------------------------------------------------

test("format strings: sections, grouping, decimals, percent, thousands scaling and literals", () => {
  assert.equal(f.formatWithString(1234.5, "#,0"), "1,235");
  assert.equal(f.formatWithString(1234.5, "#,0.00"), "1,234.50");
  assert.equal(f.formatWithString(0.1234, "0.0%"), "12.3%");
  assert.equal(f.formatWithString(-1500, "\\$#,0;(\\$#,0)"), "($1,500)");
  assert.equal(f.formatWithString(1500, "\\$#,0;(\\$#,0)"), "$1,500");
  assert.equal(f.formatWithString(0, "#,0;-#,0;\"zero\""), "zero");
  assert.equal(f.formatWithString(15420, '#,0,"K"'), "15K");
  assert.equal(f.formatWithString(-5, "#,0"), "-5");
  assert.equal(f.formatWithString(12, "General"), "12");
  assert.equal(f.formatWithString(0.5, "#.00"), ".50");
  assert.equal(f.isPercentFormat('0"%"'), false, "a quoted % is text");
  assert.equal(f.isPercentFormat("0.0%"), true);
});

test("display units and decimals; percentages are never scaled; axis label expressions", () => {
  assert.equal(f.formatUnits(15420, { displayUnits: 1000, decimals: 1 }), "15.4K");
  assert.equal(f.formatUnits(2.5e9, { displayUnits: 1e9, decimals: null }), "2.5bn");
  assert.equal(f.formatUnits(0.256, { displayUnits: 1000, decimals: 1, format: "0.0%" }), "25.6%");
  assert.equal(f.formatUnits(7, { format: "#,0.00" }), "7.00");
  assert.equal(f.axisUnitsExpr(1000, 1), "format(datum.value / 1000, ',.1f') + 'K'");
  assert.equal(f.axisUnitsExpr(1e6, null), "format(datum.value / 1000000, ',.3~r') + 'M'");
  assert.equal(f.axisUnitsExpr(null, null), undefined);
  assert.equal(f.axisUnitsExpr(null, 2), "format(datum.value / 1, ',.2f')");
});

test("data labels use the label's own format string before units and decimals", () => {
  assert.equal(r.formatDataLabel(1500, "1500", { format: '#,0,"K"', displayUnits: 1e6 }), "2K");
  assert.equal(r.formatDataLabel(1500, "1500", { displayUnits: 1000, decimals: 1 }), "1.5K");
});

test("the value axis takes its display units as a label expression, not for percent measures", () => {
  const built = r.buildChart(chart({ formatting: { ...chart().formatting, axes: { value: { show: true, displayUnits: 1000, decimals: 0 } } } }), theme);
  assert.equal(built.spec.encoding.y.axis.labelExpr, "format(datum.value / 1000, ',.0f') + 'K'");
  const percent = chart({ columns: [column("column", "Region", "Category"), column("measure", "Sales", "Y", { format: "0.0%" })], formatting: { ...chart().formatting, axes: { value: { displayUnits: 1000 } } } });
  assert.equal(r.buildChart(percent, theme).spec.encoding.y.axis.labelExpr, undefined);
});

// --- merge of a formatting-only render (sections 10 and 23) ------------------------------------------------

const page = (visuals, extra = {}) => ({ visuals, revision: "r1", page_style: {}, theme, ...extra });
const fmt = (extra = {}) => ({ title: "T", kind: "column", rank: 1, hidden: false, fidelity: "exact", unrendered: [], data_key: "k", columns: chart().columns, formatting: { title: { show: true } }, ...extra });

test("every data key of the cached row survives a formatting merge, even if the answer carried one", () => {
  const cached = chart({ data_key: "k", totals: { values: [null, 25], formatted: [null, "25"] }, highlights: [["West", 2]], referenceLines: [{ kind: "constant", value: 7 }], smallMultiples: { index: 0, panels: [] }, dynamic: { "title.text": "Sales 2026" }, extra: [1] });
  const { result, refetch } = r.mergeFormatting(page([cached]), { visuals: { chart: fmt({ rows: [["bogus"]], totals: null, dynamic: {} }) }, revision: "r2" });
  assert.deepEqual(refetch, []);
  const row = result.visuals[0];
  for (const key of ["rows", "totals", "highlights", "referenceLines", "smallMultiples", "dynamic", "extra"]) assert.deepEqual(row[key], cached[key], key);
  assert.deepEqual(r.DATA_KEYS, ["rows", "formatted_rows", "selected", "truncated", "sort", "extra", "totals", "highlights", "referenceLines", "smallMultiples", "dynamic", "dataNotes", "declined"]);
});

test("a declined row keeps its error, needs_desktop and fidelity none; data notes keep it approximate", () => {
  const declined = chart({ data_key: "k", declined: true, error: "Studio's engine cannot read [New column]", needs_desktop: true, fidelity: "none" });
  const one = r.mergeFormatting(page([declined]), { visuals: { chart: fmt({ fidelity: "exact" }) } }).result.visuals[0];
  assert.deepEqual([one.error, one.needs_desktop, one.fidelity, one.declined], ["Studio's engine cannot read [New column]", true, "none", true]);
  const noted = chart({ data_key: "k", dataNotes: ["Only the first 100 rows were used for the values computed here"], fidelity: "approximate" });
  assert.equal(r.mergeFormatting(page([noted]), { visuals: { chart: fmt({ fidelity: "exact" }) } }).result.visuals[0].fidelity, "approximate");
  assert.equal(r.mergeFormatting(page([noted]), { visuals: { chart: fmt({ fidelity: "none" }) } }).result.visuals[0].fidelity, "none", "worse stays worse");
});

test("the envelope keys of the answer replace the cached ones, a null included", () => {
  const cached = page([chart({ data_key: "k" })], { bookmarks: [{ name: "a" }], bookmark: { name: "a" }, mobile: { visuals: {} }, notes: ["old"], drillthroughs: [] });
  const { result } = r.mergeFormatting(cached, { visuals: { chart: fmt() }, bookmark: null, mobile: null, notes: [], interactions: { chart: {} } });
  assert.equal(result.bookmark, null);
  assert.equal(result.mobile, null);
  assert.deepEqual(result.notes, []);
  assert.deepEqual(result.bookmarks, [{ name: "a" }], "a key the answer omits stays");
  assert.deepEqual(result.interactions, { chart: {} });
});

// --- fidelity with the server's reasons ------------------------------------------------------------------

test("data notes make an honest badge with the server's own words", () => {
  const note = r.fidelityNote({ kind: "column", fidelity: "approximate", unrendered: ["total.totals"], dataNotes: ["The totals, highlights or whole-visual values of this visual could not be computed here"] });
  assert.equal(note.data, true);
  assert.equal(note.label, "Values may differ from Power BI");
  assert.match(note.detail, /could not be computed here\. Not drawn here: total: totals\./);
  const declined = r.fidelityNote({ kind: "table", fidelity: "none", error: "This visual reads 'Margin', which Power BI reports an error in", needs_desktop: true, declined: true });
  assert.equal(declined.level, "desktop");
  assert.equal(declined.label, "This visual reads 'Margin', which Power BI reports an error in");
});

// --- dynamic titles and backgrounds (section 21) -----------------------------------------------------------

test("a measure's title text, colors and background replace the static tokens", () => {
  const visual = chart({ formatting: { title: { show: true, text: "", textSource: "dynamic", fontColor: "#111111" }, container: { background: "#ffffffff" }, dynamicProperties: ["title.text", "title.fontColor", "background.color"] }, dynamic: { "title.text": "Sales 2026", "title.fontColor": "#00FF00", "background.color": "#FFEE00" } });
  const view = r.titleView(visual);
  assert.deepEqual([view.text, view.style.color, view.dynamic], ["Sales 2026", "#00FF00", true]);
  assert.equal(r.effectiveContainer(visual).background, "#FFEE00");
  assert.equal(r.effectiveContainer({ ...visual, dynamic: { "background.color": "red; x" } }).background, "#ffffffff", "an unsafe value is ignored");
});

// --- tables: conditional formatting, icons, bars, totals (sections 12 and 14) -----------------------------

const cfTable = () => ({
  id: "t", kind: "table", title: "t",
  columns: [
    column("column", "Region", "Values", { colorIndex: 3 }),
    column("measure", "Sales", "Values", { backgroundIndex: 2, icon: { index: 4, layout: "after", alignment: "top" } }),
    column("computed", "values.backColor", "Formatting", { hidden: true, computed: "gradient", query_ref: null }),
    column("computed", "values.fontColor", "Formatting", { hidden: true, computed: "rules", query_ref: null }),
    column("computed", "values.icon", "Formatting", { hidden: true, computed: "rules", query_ref: null }),
  ],
  rows: [["A", 0, "#000000", "#ff0000", "ColoredArrowDown"], ["B", 5, "#808080", null, null], ["C", 10, null, null, "Bogus"]],
  formatting: { table: { columns: { "Orders.Region": { fontColor: "#123456", background: "#eeeeee" } }, total: { show: true, label: "Total", bold: true, color: "#252423", fontSize: 13.33 } } },
});

test("cells read their conditional colors by index, over the column's own plain colors", () => {
  const t = cfTable();
  assert.deepEqual(c.cellLook(t, 0, 0), { color: "#ff0000", background: "#eeeeee" }, "a rule color wins over the column's font color");
  assert.deepEqual(c.cellLook(t, 1, 0), { color: "#123456", background: "#eeeeee" }, "no rule for this row: the column's own colors");
  assert.deepEqual(c.cellLook(t, 0, 1), { background: "#000000", icon: { name: "ColoredArrowDown", layout: "after", alignment: "top" } });
  assert.deepEqual(c.cellLook(t, 2, 1), { icon: { name: "Bogus", layout: "after", alignment: "top" } }, "the name is passed on; the glyph map decides");
  assert.equal(c.colorAt(["x", "javascript:alert(1)"], 1), undefined);
});

test("icon names map to glyphs; an unknown name draws nothing", () => {
  assert.deepEqual(c.iconGlyph("ColoredArrowUp"), { shape: "arrow", angle: -90, color: "#1AAB40" });
  assert.equal(c.iconGlyph("GreyArrowDownRight").angle, 45);
  assert.deepEqual(c.iconGlyph("CircleRedBlackBackgroundPatternFill"), { shape: "circle", color: "#D64550", rim: "dark" });
  assert.equal(c.iconGlyph("CircleSymbolHigh").symbol, "check");
  assert.deepEqual(c.iconGlyph("StarMediumLight"), { shape: "star", color: "#F2B200", fill: 0.5 });
  assert.deepEqual(c.iconGlyph("SignalBarMedium2Colored"), { shape: "bars", fill: 3, color: "#118DFF" });
  assert.equal(c.iconGlyph("Circle75").fill, 0.25);
  assert.equal(c.iconGlyph("Quadrant50Colored").fill, 0.5);
  assert.equal(c.iconGlyph("TriangleMedium").shape, "dash");
  for (const bad of ["Bogus", "", null, 5, "x".repeat(100)]) assert.equal(c.iconGlyph(bad), null, String(bad));
});

test("every icon of Desktop's icon sets has a glyph", () => {
  const sets = ["SignLow", "SignMedium", "CircleHigh", "CircleRedPatternFill", "CircleYellowPatternFill", "CircleGreenPatternFill", "CircleRedBlackBackgroundPatternFill", "CircleYellowBlackOutlinePatternFill",
    "ColoredArrowDown", "ColoredArrowRight", "ColoredArrowUp", "GreyArrowDown", "GreyArrowUpRight", "FlagRedPatternFill", "FlagGreenPatternFill", "CircleSymbolLow", "SymbolMedium", "StarLow", "StarHigh", "StarHighLight",
    "TriangleLow", "TriangleHigh", "ColoredArrowDownRight", "ColoredArrowUpRight", "CircleBlackFill", "CircleGrayPatternFill", "CirclePurplePatternFill", "SignalBarEmpty", "SignalBarLow", "SignalBarFull", "SignalBarLowColored",
    "CircleEmpty", "CircleHalf", "Circle25", "CircleFilled", "QuadrantEmpty", "Quadrant25", "Quadrant75", "QuadrantFull", "QuadrantFullColored", "CircleMedium", "CircleLow", "CircleGreenBlackOutlinePatternFill"];
  for (const name of sets) assert.ok(c.iconGlyph(name), name);
});

test("data bars: bounds from the data with zero, explicit bounds, negatives, the axis and reversed bars", () => {
  const bars = { positive: "#00ff00", negative: "#ff0000", hideText: false, axis: null, reverse: false, min: null, max: null };
  const visual = { columns: [column("measure", "Sales", "Values", { dataBars: bars })], rows: [[-5], [5], [15]] };
  const range = c.dataBarRange(visual, 0);
  assert.deepEqual(range, { min: -5, max: 15 });
  assert.deepEqual(c.dataBarGeometry(15, bars, range), { left: 25, width: 75, axis: 25, color: "#00ff00", negative: false });
  assert.deepEqual(c.dataBarGeometry(-5, bars, range), { left: 0, width: 25, axis: 25, color: "#ff0000", negative: true });
  const reversed = c.dataBarGeometry(15, { ...bars, reverse: true }, range);
  assert.deepEqual([reversed.left, reversed.width, reversed.axis], [0, 75, 75]);
  assert.deepEqual(c.dataBarRange({ columns: [column("measure", "S", "V", { dataBars: { ...bars, min: 0, max: 10 } })], rows: [[50]] }, 0), { min: 0, max: 10 });
  const clamped = c.dataBarGeometry(50, bars, { min: 0, max: 10 });
  assert.deepEqual([clamped.left, clamped.width, clamped.axis], [0, 100, null]);
});

test("the totals row: the label in the first drawn column, formatted totals, the total token's font", () => {
  const t = { ...cfTable(), totals: { values: [null, 15, null], formatted: [null, "15", null] } };
  const view = c.totalsView(t, [0, 1]);
  assert.deepEqual(view.cells, [{ index: 0, text: "Total", numeric: false }, { index: 1, text: "15", numeric: true }]);
  assert.equal(view.style.fontWeight, 700);
  assert.equal(view.style.fontSize, 13.33);
  assert.equal(c.totalsView({ ...t, formatting: { table: { total: { show: false } } } }, [0, 1]), null, "the switch is off");
  assert.equal(c.totalsView({ ...t, totals: undefined }, [0, 1]), null, "no totals under a measure filter");
});

test("tables: grid weights and word wrap come from the token", () => {
  const view = r.tableView({ grid: { horizontal: true, horizontalWeight: 2, vertical: true, verticalWeight: null }, header: { wrap: false }, values: { wrap: true } }, undefined);
  assert.deepEqual([view.horizontalWeight, view.verticalWeight, view.headerWrap, view.valuesWrap], [2, 1, false, true]);
  assert.deepEqual([r.tableView({}, undefined).headerWrap, r.tableView({}, undefined).valuesWrap], [true, false], "Power BI wraps headers, not values, by default");
});

test("cards take their conditional value, caption and tile colors from the first row", () => {
  const card = { rows: [[5, "#ff0000", "#00ff00", "#0000ff"]], formatting: { cardValue: { colorIndex: 1 }, cardLabel: { colorIndex: 2 }, tile: { default: { fillIndex: 3 } } } };
  assert.deepEqual(c.cardColors(card), { value: "#ff0000", label: "#00ff00", fill: "#0000ff" });
  assert.deepEqual(c.cardColors({ rows: [[5, null]], formatting: { cardValue: { colorIndex: 1 } } }), {});
});

test("a stepped matrix: one indented header column, parent lines when an outer group changes", () => {
  const m = { columns: [column("column", "Region", "Rows"), column("column", "City", "Rows"), column("measure", "Sales", "Values")], rows: [["West", "LA", 1], ["West", "SF", 2], ["East", "NY", 3]] };
  const { groups, values, rows } = c.steppedRows(m);
  assert.deepEqual([groups, values], [[0, 1], [2]]);
  assert.deepEqual(rows.map(line => [line.level, line.label, line.row]), [[0, "West", null], [1, "LA", 0], [1, "SF", 1], [0, "East", null], [1, "NY", 2]]);
});

// --- charts: rule colors, visual calculations, highlights, analytics lines --------------------------------

test("a conditional data color wins over every other color, per series", () => {
  const visual = chart({
    columns: [...chart().columns, column("computed", "dataPoint.fill", "Formatting", { hidden: true, computed: "rules", query_ref: null })],
    rows: [["West", 5, "#ff0000"], ["East", 12, null], ["North", 8, "#00ff00"]],
    formatting: { ...chart().formatting, series: { defaultColor: "#1E90FF", categoryColors: [{ value: "East", color: "#cccccc" }], ruleColors: [{ metadata: "Orders.Sales", index: 2 }] } },
  });
  const plan = r.planChart(visual, theme);
  assert.deepEqual(plan.points.map(p => p.color), ["#ff0000", "#cccccc", "#00ff00"]);
  assert.equal(plan.matched, true);
  const other = { ...visual, formatting: { ...visual.formatting, series: { ...visual.formatting.series, ruleColors: [{ metadata: "Orders.Profit", index: 2 }] } } };
  assert.deepEqual(r.planChart(other, theme).points.map(p => p.color), ["#1E90FF", "#cccccc", "#1E90FF"], "a rule for another series does not apply");
});

test("visual calculations are values a chart draws; hidden SortBy columns are not categories", () => {
  const visual = chart({
    columns: [column("column", "MonthNumber", "SortBy", { hidden: true }), column("column", "Month", "Category"), column("computed", "Running", "Y", { computed: "visualCalculation", query_ref: "Running" })],
    rows: [[1, "Jan", 5], [2, "Feb", 9]], formatted_rows: [],
  });
  assert.deepEqual(r.valueColumns(visual), [2]);
  assert.equal(r.categoryColumn(visual), 1);
  assert.deepEqual(r.planChart(visual, theme).points.map(p => [p.categoryLabel, p.value, p.row]), [["Jan", 5, 0], ["Feb", 9, 1]]);
});

test("highlights: the full bar dimmed and the highlighted part on top in full color", async () => {
  const visual = chart({ highlights: [["West", 2], ["East", null], ["North", 8]] });
  const plan = r.planChart(visual, theme);
  assert.deepEqual(plan.points.map(p => [p.highlight, p.opacity]), [[2, 1], [null, r.DIMMED], [8, 1]]);
  const built = r.buildChart(visual, theme);
  assert.equal(built.spec.layer[0].mark.opacity, r.DIMMED);
  assert.deepEqual(built.spec.layer[1].transform, [{ filter: "isValid(datum.highlight)" }]);
  assert.equal(built.spec.layer[1].mark.opacity, 1);
  assert.equal(built.spec.layer[1].encoding.y.field, "highlight");
  const { warnings, svg } = await draw(built);
  assert.deepEqual(warnings, []);
  assert.equal((svg.match(/<path[^>]*aria-roledescription="bar"/g) ?? []).length, 5, "three dimmed bars and two highlighted parts");
});

test("highlights on pies and stacked bars dim what the selection does not reach", async () => {
  const pie = r.buildChart(chart({ kind: "pie", highlights: [["West", 5], ["East", null], ["North", null]] }), theme);
  assert.deepEqual(pie.spec.encoding.opacity, { field: "opacity", type: "quantitative", scale: null, legend: null });
  assert.deepEqual((await draw(pie)).warnings, []);
  const stacked = r.buildChart(chart({
    columns: [column("column", "Region", "Category"), column("column", "Segment", "Series"), column("measure", "Sales", "Y")],
    rows: [["West", "A", 5], ["West", "B", 7]], highlights: [["West", "A", 5], ["West", "B", null]],
    formatting: { ...chart().formatting, chart: { stacking: "stacked" } },
  }), theme);
  assert.equal(stacked.spec.encoding.opacity.field, "opacity");
  assert.deepEqual((await draw(stacked)).warnings, []);
});

test("no highlights: the spec is unchanged (a single mark, no opacity)", () => {
  const built = r.buildChart(chart(), theme);
  assert.equal(built.spec.mark.type, "bar");
  assert.equal(built.spec.mark.opacity, undefined);
  assert.equal(built.spec.layer, undefined);
});

const line = (extra = {}) => ({ id: "0", object: "y1AxisReferenceLine", kind: "constant", axis: "value", series: "Orders.Sales", value: 7, name: "Target", color: "#FF0000", alpha: 0.5, style: "dotted", width: 2, position: "front", label: { show: true, text: "ValueAndName", color: null, decimals: 1, displayUnits: null, horizontal: "right", vertical: "above" }, ...extra });

test("analytics lines: a constant rule with its style, label and tooltip, switching off inherited channels", async () => {
  const built = r.buildChart(chart({ referenceLines: [line()] }), theme);
  const [main, rule, label] = built.spec.layer;
  assert.deepEqual(main, { mark: built.spec.layer[0].mark });
  assert.deepEqual(rule.data, { values: [{ ruleValue: 7, text: "Target 7.0" }] });
  assert.deepEqual(rule.mark, { type: "rule", color: "#FF0000", strokeWidth: 2, strokeDash: [1, 3], strokeCap: "round", opacity: 0.5, tooltip: true });
  assert.equal(rule.encoding.x, null, "the rule spans the plot instead of standing on a category");
  assert.equal(rule.encoding.tooltip.value, "Target: Target 7.0");
  assert.deepEqual(rule.encoding.y, { field: "ruleValue", type: "quantitative" });
  assert.deepEqual(label.encoding.x, { value: "width" });
  assert.equal(label.mark.align, "right");
  const { warnings, svg } = await draw(built);
  assert.deepEqual(warnings, []);
  assert.match(svg, /stroke="#FF0000"/);
  assert.match(svg, />Target 7\.0</);
});

test("analytics lines: back lines go under the marks; trend lines from first to last category; X-axis lines at their category", async () => {
  const lines = [
    line({ position: "back", label: { show: false } }),
    { id: null, object: "trend", kind: "trend", value: null, start: 4, end: 11, series: null, color: null, style: "dashed", label: { show: false } },
    { id: "2", object: "xAxisReferenceLine", kind: "constant", axis: "category", value: "East", color: "#0000FF", label: { show: true, text: "Name" }, name: "Launch" },
    { id: "3", kind: "average", axis: "value", value: null, label: { show: false } },
  ];
  const built = r.buildChart(chart({ referenceLines: lines }), theme);
  const types = built.spec.layer.map(layer => layer.mark.type);
  assert.deepEqual(types, ["rule", "bar", "line", "rule", "text"], "a line with no value (nothing to compute from) is not drawn");
  assert.deepEqual(built.spec.layer[2].data.values, [{ categoryLabel: "West", ruleValue: 4 }, { categoryLabel: "North", ruleValue: 11 }]);
  assert.equal(built.spec.layer[2].mark.color, "#0B6E4F", "a combined trend line takes the theme's first color");
  assert.deepEqual(built.spec.layer[3].data.values, [{ categoryLabel: "East", text: "Launch" }]);
  assert.deepEqual(built.spec.encoding.x.scale, { domain: ["West", "East", "North"] }, "the axis keeps the drawn order");
  const { warnings, svg } = await draw(built);
  assert.deepEqual(warnings, []);
  assert.match(svg, /stroke="#0000FF"/);
});

test("analytics lines on horizontal bars and stacked bars; a per-measure trend takes its series color", async () => {
  const bar = r.buildChart(chart({ kind: "bar", referenceLines: [line({ label: { show: true, text: "Value", horizontal: "left", vertical: "under" } })] }), theme);
  assert.deepEqual(bar.spec.layer[1].encoding.x, { field: "ruleValue", type: "quantitative" });
  assert.equal(bar.spec.layer[1].encoding.y, null);
  assert.deepEqual((await draw(bar)).warnings, []);
  const stacked = r.buildChart(chart({
    columns: [column("column", "Region", "Category"), column("column", "Segment", "Series"), column("measure", "Sales", "Y")],
    rows: [["West", "A", 5], ["West", "B", 7], ["East", "A", 2], ["East", "B", 1]], formatting: { ...chart().formatting, chart: { stacking: "stacked" }, series: { seriesColors: { "Orders.Sales": "#AA0000" } } },
    referenceLines: [line({ label: { show: false } }), { kind: "trend", object: "trend", series: "Orders.Sales", start: 1, end: 2, label: { show: false } }],
  }), theme);
  assert.equal(stacked.spec.layer[1].encoding.y2, null, "the bars' range is not inherited");
  assert.equal(stacked.spec.layer[2].mark.color, "#AA0000");
  assert.deepEqual((await draw(stacked)).warnings, []);
});

test("a visual with a report-page tooltip draws no Vega tooltip", () => {
  const built = r.buildChart(chart({ tooltipPage: { page: "tip", width: 320, height: 240 } }), theme, { tooltip: false });
  assert.equal(built.spec.encoding.tooltip, undefined);
  assert.equal(built.spec.mark.tooltip, false);
});

test("line labels: value, name or both, with the label's decimals and units", () => {
  assert.equal(r.lineText({ kind: "average", value: 1234.5, label: { text: "Value", displayUnits: 1000, decimals: 1 } }), "1.2K");
  assert.equal(r.lineText({ kind: "average", value: 5, label: { text: "Name" } }), "Average line");
  assert.equal(r.lineText({ kind: "max", name: "Peak", value: 5, label: { text: "ValueAndName" } }, "#,0.00"), "Peak 5.00");
  assert.equal(r.lineText({ kind: "trend", value: null, label: { text: "Value" } }), "Trend line");
});

// --- small multiples (section 21) ------------------------------------------------------------------------

test("small multiples: one chart per panel with the split column hidden, a shared axis and a grid", async () => {
  const visual = chart({
    columns: [column("column", "Year", "Rows"), column("column", "Region", "Category"), column("measure", "Sales", "Y")],
    rows: [[2025, "West", 5], [2025, "East", 3], [2026, "West", 9], [2026, "East", -2]], formatted_rows: [["2025", null, "5"], ["2025", null, "3"], ["2026", null, "9"], ["2026", null, "-2"]],
    formatting: { ...chart().formatting, smallMultiples: { index: 0, field: "Year", layout: { rows: null, columns: null } } },
    smallMultiples: { index: 0, field: "Year", values: [2025, 2026], panels: [{ value: 2025, rows: [0, 1] }, { value: 2026, rows: [2, 3, 99] }] },
  });
  const panels = r.smallMultiplePanels(visual);
  assert.deepEqual(panels.map(p => [p.label, p.visual.rows.length]), [["2025", 2], ["2026", 2]]);
  assert.equal(panels[0].visual.columns[0].hidden, true);
  assert.equal(r.categoryColumn(panels[0].visual), 1);
  assert.deepEqual(r.sharedDomain(panels, theme), [-2, 9]);
  assert.deepEqual(r.panelGrid(5, null), { columns: 3, rows: 2 });
  assert.deepEqual(r.panelGrid(4, { columns: 4 }), { columns: 4, rows: 1 });
  const built = r.buildChart(panels[1].visual, theme, { valueDomain: [-2, 9] });
  assert.deepEqual(built.spec.encoding.y.scale, { domain: [-2, 9] });
  assert.deepEqual((await draw(built)).warnings, []);
  assert.deepEqual(r.smallMultiplePanels(chart()), []);
});

// --- buttons, navigators and actions (section 16) --------------------------------------------------------

test("button state precedence: disabled, pressed (selected), hover, default; navigator: press, hover, selected", () => {
  assert.equal(a.buttonStateName({ disabled: true, pressed: true, hovered: true }), "disabled");
  assert.equal(a.buttonStateName({ pressed: true, hovered: true }), "selected");
  assert.equal(a.buttonStateName({ hovered: true }), "hover");
  assert.equal(a.buttonStateName({}), "default");
  assert.equal(a.navigatorStateName({ pressed: true, hovered: true, selected: true }), "press");
  assert.equal(a.navigatorStateName({ hovered: true, selected: true }), "hover");
  assert.equal(a.navigatorStateName({ selected: true }), "selected");
  const token = { states: { default: { fill: "#111111ff" }, hover: { fill: "#222222ff" }, selected: null, disabled: null } };
  assert.equal(a.stateToken(token, "hover").fill, "#222222ff");
  assert.equal(a.stateToken(token, "selected").fill, "#111111ff", "an unstyled state draws the default");
});

test("a button's box: fill, outline, corner shape and text placement from its state", () => {
  const style = a.buttonBoxStyle({ shape: "rectangleRounded", radius: 10 }, { fill: "#112233ff", outline: { show: true, color: "#605E5C", width: 2 }, text: { alignment: "left", verticalAlignment: "bottom" } });
  assert.deepEqual(style, { backgroundColor: "#112233ff", border: "2px solid #605E5C", borderRadius: 10, justifyContent: "flex-start", alignItems: "flex-end" });
  assert.equal(a.buttonBoxStyle({ shape: "pill" }, { outline: { show: false } }).border, "none");
  assert.equal(a.buttonTextStyle({ text: { fontSize: 16, color: "#252423", underline: true } }).textDecoration, "underline");
  assert.equal(a.navigatorGridStyle({ layout: { orientation: "vertical" } }, 3).gridTemplateColumns, "minmax(0, 1fr)");
  assert.equal(a.navigatorGridStyle({ layout: { orientation: "grid", columns: 2 } }, 4).gridTemplateColumns, "repeat(2, minmax(0, 1fr))");
  assert.equal(a.navigatorGridStyle({ layout: { orientation: "horizontal" } }, 3).gridTemplateColumns, "repeat(3, minmax(0, 1fr))");
});

test("actions: what they do, and when a button is disabled", () => {
  assert.deepEqual(a.actionOutcome({ type: "page", target: "p2", valid: true }), { kind: "navigate", page: "p2" });
  assert.deepEqual(a.actionOutcome({ type: "bookmark", target: "Bookmark1" }), { kind: "bookmark", name: "Bookmark1" });
  assert.deepEqual(a.actionOutcome({ type: "back" }), { kind: "back" });
  assert.deepEqual(a.actionOutcome({ type: "drillthrough", target: "dt" }), { kind: "drillthrough", page: "dt" });
  assert.deepEqual(a.actionOutcome({ type: "url", url: "https://example.com/a" }), { kind: "url", url: "https://example.com/a" });
  assert.deepEqual(a.actionOutcome({ type: "url", url: "javascript:alert(1)" }), { kind: "none" });
  assert.deepEqual(a.actionOutcome({ type: "qna", target: null }), { kind: "message", text: "This button's action works in Power BI only" });
  assert.deepEqual(a.actionOutcome({ type: "unknown" }), { kind: "message", text: a.POWER_BI_ONLY });
  assert.deepEqual(a.actionOutcome(null), { kind: "none" });
  assert.equal(a.actionState({ type: "page", valid: false }, true).disabled, true);
  assert.equal(a.actionState({ type: "drillthrough", valid: true }, false).disabled, true);
  assert.match(a.actionState({ type: "drillthrough", valid: true }, false).reason, /Select a data point/);
  assert.equal(a.actionState({ type: "drillthrough", valid: true }, true).disabled, false);
  assert.equal(a.actionState({ type: "qna", valid: false }, false).disabled, false, "qna and unknown stay enabled");
  assert.equal(a.actionState(undefined, false).disabled, false);
});

// --- drillthrough and tooltips (section 18) ----------------------------------------------------------------

const targets = [
  { page: "store", title: "Store details", fields: [{ field: "Orders.Region" }], keepAllFilters: true },
  { page: "segment", title: "Segment", fields: [{ field: "Orders.Segment" }], keepAllFilters: false },
  { page: "here", title: "Self", fields: [{ field: "Orders.Region" }] },
];

test("drillthrough targets: pages whose bound field is a column of the row, never the page itself", () => {
  assert.deepEqual(a.drillTargets(targets, chart(), "here").map(t => t.page), ["store"]);
  const hiddenTooltip = chart({ columns: [...chart().columns, column("column", "Segment", "Tooltips", { hidden: true })], rows: [["West", 5, "A"]] });
  assert.deepEqual(a.drillTargets(targets, hiddenTooltip, "here").map(t => t.page), ["store", "segment"], "a hidden Tooltips field counts");
  assert.deepEqual(a.drillFiltersFor(targets[0], chart(), 1), [{ field: "Orders.Region", values: ["East"] }]);
  assert.deepEqual(a.drillTargets(undefined, chart()), []);
});

test("a drillthrough button needs a selected data point in a visual with the bound field; slicers do not count", () => {
  const slicer = { ...chart(), id: "s", kind: "slicer" };
  assert.equal(a.selectionDrill(targets[0], [slicer, chart()], { s: ["West"] }), null);
  assert.deepEqual(a.selectionDrill(targets[0], [slicer, chart()], { chart: ["East"] }), [{ field: "Orders.Region", values: ["East"] }]);
  assert.equal(a.selectionDrill(targets[0], [chart()], { chart: [] }), null);
  assert.equal(a.selectionDrill(undefined, [chart()], { chart: ["East"] }), null);
});

test("the drill request carries the source page's context only when the target keeps all filters", () => {
  const source = { page: "p1", visual: "chart", selections: [{ visual_id: "s", values: ["West"] }] };
  assert.deepEqual(a.drillRequest(targets[0], [{ field: "Orders.Region", values: ["East"] }], source), { drill: [{ field: "Orders.Region", values: ["East"] }], drill_source: source });
  assert.deepEqual(a.drillRequest(targets[1], [{ field: "Orders.Segment", values: ["A"] }], source), { drill: [{ field: "Orders.Segment", values: ["A"] }] });
  assert.equal(a.describeDrill([{ field: "DimStore.StoreName", values: ["Store 1", "Store 2"] }]), "StoreName is Store 1, Store 2");
});

test("tooltip pages: the hovered point's category and series, a cache key, a scale and a position that stays inside", () => {
  const visual = chart({ columns: [column("column", "Region", "Category"), column("column", "Segment", "Series"), column("measure", "Sales", "Y"), column("column", "Hidden", "Tooltips", { hidden: true })], rows: [["West", "A", 5, "x"]] });
  assert.deepEqual(a.tooltipDrill(visual, 0), [{ field: "Orders.Region", values: ["West"] }, { field: "Orders.Segment", values: ["A"] }]);
  assert.deepEqual(a.tooltipDrill(visual, 5), []);
  assert.notEqual(a.tooltipKey("tip", [{ field: "a", values: [1] }], null, "h1"), a.tooltipKey("tip", [{ field: "a", values: [1] }], null, "h2"));
  assert.equal(a.tooltipScale({ width: 320, height: 240 }), 1);
  assert.equal(a.tooltipScale({ width: 800, height: 400 }), 0.5);
  assert.deepEqual(a.floatingPosition({ x: 10, y: 10 }, { width: 100, height: 50 }, { width: 1000, height: 800 }), { left: 24, top: 24 });
  assert.deepEqual(a.floatingPosition({ x: 950, y: 790 }, { width: 100, height: 50 }, { width: 1000, height: 800 }), { left: 836, top: 750 });
});

test("a data point reference names the point and carries its values by field", () => {
  const ref = a.dataPointRef(chart(), 1, 1);
  assert.deepEqual(ref, { visualId: "chart", row: 1, column: 1, label: "East · 12", values: { "Orders.Region": "East", "Orders.Sales": 12 } });
  assert.equal(a.rowOfCategory(chart(), "North"), 2);
  assert.equal(a.rowOfCategory(chart(), "Nowhere"), -1);
});

// --- synced slicers, pending views, bookmarks, phone layout (sections 17, 19, 20) -------------------------

test("synced slicers share a selection across pages only when the group shares filters", () => {
  a.resetSync();
  a.rememberSyncGroups("ws", { Region: [{ page: "p1", visual: "s1" }, { page: "p2", visual: "s2", hidden: true }], Field: [{ page: "p1", visual: "f1" }] });
  assert.equal(a.sharedGroup({ syncGroup: { groupName: "Region", fieldChanges: true, filterChanges: true } }), "Region");
  assert.equal(a.sharedGroup({ syncGroup: { groupName: "Field", fieldChanges: true, filterChanges: false } }), null);
  assert.equal(a.sharedGroup({}), null);
  a.recordSyncSelection("ws", "Region", ["West"]);
  assert.deepEqual(a.syncedSelections("ws", "p2"), { s2: ["West"] }, "a hidden synced slicer still filters its page");
  assert.deepEqual(a.syncedSelections("ws", "p1"), { s1: ["West"] });
  assert.deepEqual(a.syncedSelections("other", "p2"), {}, "workspaces are separate");
  a.clearSyncSelections("ws", ["Region"]);
  assert.deepEqual(a.syncedSelections("ws", "p2"), {});
});

test("a page opened by an action finds what it should show", () => {
  a.stashView("ws", "store", { drill: [{ field: "Orders.Region", values: ["East"] }], from: "p1" });
  assert.equal(a.peekView("ws", "store").from, "p1");
  assert.equal(a.peekView("ws", "p1"), undefined);
  assert.deepEqual(Object.keys(a.peekView("ws", "store")).sort(), ["drill", "from"], "the stash time is not part of the view");
  a.dropView("ws", "store");
  assert.equal(a.peekView("ws", "store"), undefined);
  a.stashView("ws", "late", { bookmark: "B1" }, 1000);
  assert.equal(a.peekView("ws", "late", 1000 + a.PENDING_VIEW_MS + 1), undefined, "a view nobody opened soon after is ignored");
});

test("bookmarks: found by name or display name; another page opens first; data captures reset selections", () => {
  const bookmarks = [{ name: "B1", displayName: "Channel focus", page: "p2", captures: { data: true, display: true, page: true } }, { name: "B2", displayName: "Look", page: "p2", captures: { data: false, display: true, page: false } }];
  assert.equal(a.findBookmark(bookmarks, "Channel focus").name, "B1");
  assert.equal(a.findBookmark(bookmarks, "B2").displayName, "Look");
  assert.equal(a.findBookmark(bookmarks, "nope"), undefined);
  assert.deepEqual(a.bookmarkPlan(bookmarks[0], "p1"), { page: "p2", resetSelections: true });
  assert.deepEqual(a.bookmarkPlan(bookmarks[0], "p2"), { page: null, resetSelections: true });
  assert.deepEqual(a.bookmarkPlan(bookmarks[1], "p1"), { page: null, resetSelections: false }, "a bookmark that does not capture the page applies here");
});

test("the phone layout draws only the placed visuals at their phone boxes, stacked by phone z", () => {
  const visuals = [{ id: "a", kind: "card" }, { id: "b", kind: "column", hidden: true }, { id: "c", kind: "textbox" }];
  const phone = a.phoneLayout({ visuals: { c: { x: 0, y: 400, width: 320, height: 200, z: 1 }, a: { x: 0, y: 0, width: 320, height: 120, z: 2 }, gone: { x: 0, y: 0, width: 1, height: 1, z: 0 } } }, visuals);
  assert.equal(phone.width, 320);
  assert.equal(phone.height, 600);
  assert.deepEqual(phone.rects.map(rect => [rect.id, rect.rank, rect.kind]), [["c", 1, "textbox"], ["a", 2, "card"]]);
  assert.equal(a.phoneLayout(null, visuals), null);
  assert.equal(a.phoneLayout({ visuals: {} }, visuals), null);
});

test("the render request: selections, searches, and bookmark, drill, source and highlight only when set", () => {
  assert.deepEqual(a.renderBody({ filters: {}, searches: {} }), { selections: [], searches: {} });
  const drill = Array.from({ length: 30 }, (_, i) => ({ field: `T.C${i}`, values: [i] }));
  const body = a.renderBody({ filters: { s: ["West"] }, searches: { s: "we" }, bookmark: "B1", drill, drillSource: { page: "p1" }, highlight: true });
  assert.deepEqual(body.selections, [{ visual_id: "s", values: ["West"] }]);
  assert.equal(body.bookmark, "B1");
  assert.equal(body.drill.length, 24, "the server takes at most 24 drill fields");
  assert.deepEqual(body.drill_source, { page: "p1" });
  assert.equal(body.highlight, true);
});
