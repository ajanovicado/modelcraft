import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const require = createRequire(import.meta.url);
async function load(file) {
  const path = new URL(`../src/${file}`, import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
  const compiled = await build({ entryPoints: [path], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", compiled.outputFiles[0].text)(require, loaded, loaded.exports);
  return loaded.exports;
}
const r = await load("reportRender.ts");
const { mergeConfig } = await load("PreviewChart.tsx");

// Fixtures follow docs/imported-report-render-contract.md.
const theme = { name: "Fixture theme", dataColors: ["#0B6E4F", "#F26419", "#33658A"], palette: ["#ffffff", "#000000", "#0b6e4f", "#f26419", "#33658A"], foreground: "#252423", background: "#FFFFFF" };
const column = (kind, name, role, extra = {}) => ({ kind, table: "Orders", name, label: name, role, query_ref: `Orders.${name}`, format: "", alignment: "", colorIndex: null, ...extra });
const container = (extra = {}) => ({ background: "#ffffffff", border: { show: false, color: null, width: null, radius: 0 }, shadow: false, padding: null, ...extra });
const chart = (extra = {}) => ({
  id: "chart", title: "Sales by Region", kind: "column", visual_type: "clusteredColumnChart", container_kind: "visual",
  position: { x: 20, y: 30, width: 400, height: 240 }, z: 1000, rank: 3, hidden: false, hidden_own: false, parent_group: null, group_children: [],
  fidelity: "approximate", unrendered: ["categoryAxis.innerPadding"], data_key: "3c6936ab",
  columns: [column("column", "Region", "Category"), column("measure", "Sales", "Y")],
  rows: [["West", 5], ["East", 12], ["North", 8]], formatted_rows: [[null, "5"], [null, "12"], [null, "8"]], selected: [], truncated: false,
  sort: [{ index: 0, descending: false }],
  formatting: { title: { show: true, text: "Sales by Region", textSource: "automatic" }, container: container(), chart: { orientation: "vertical", stacking: "clustered" }, series: { defaultColor: null, seriesColors: {}, categoryColors: [], varyByCategory: false }, labels: { show: false }, legend: { show: true, position: "top" }, axes: {} },
  ...extra,
});
const withSeries = (extra = {}) => chart({
  columns: [column("column", "Region", "Category"), column("column", "Segment", "Series", { query_ref: "Orders.Segment" }), column("measure", "Sales", "Y")],
  rows: [["West", "A", 5], ["West", "B", 7], ["East", "A", 12], ["East", "B", 3]], formatted_rows: [], ...extra,
});

// --- safe values ------------------------------------------------------------------------------------

test("colors, fonts and images are re-validated before they reach a style object", () => {
  assert.equal(r.safeColor("#0B6E4F"), "#0B6E4F");
  assert.equal(r.safeColor("#ff880099"), "#ff880099");
  for (const bad of ["red", "url(https://evil)", "#12", "#gggggg", "rgb(0,0,0)", "#000000; background:url(x)", null, undefined, 5]) assert.equal(r.safeColor(bad), undefined, String(bad));
  assert.equal(r.safeFont('"Segoe UI Semibold", wf_segoe-ui_semibold, helvetica, arial, sans-serif'), '"Segoe UI Semibold", wf_segoe-ui_semibold, helvetica, arial, sans-serif');
  for (const bad of ["Arial; color: red", "a{b}", "url(x)", "", "x".repeat(300)]) assert.equal(r.safeFont(bad), undefined, bad.slice(0, 20));
  assert.equal(r.safeImage("data:image/png;base64,iVBORw0KGgo="), "data:image/png;base64,iVBORw0KGgo=");
  for (const bad of ["https://example.com/a.png", "javascript:alert(1)", "data:text/html;base64,PHNjcmlwdD4=", "data:image/png;base64,AAAA\")", null]) assert.equal(r.safeImage(bad), undefined, String(bad));
});

test("alpha is folded into hex colors and can be dropped again", () => {
  assert.equal(r.withAlpha("#ffffff", 0), "#ffffff00");
  assert.equal(r.withAlpha("#123456", 1), "#123456ff");
  assert.equal(r.withAlpha("#ff000080", 0.5), "#ff000040");
  assert.equal(r.withAlpha("nope", 1), undefined);
  assert.equal(r.opaque("#ff880099"), "#ff8800");
  assert.equal(r.opaque("#abc"), "#aabbcc");
});

// --- palette -----------------------------------------------------------------------------------------

test("palette resolution: the report's own colors, Power BI's defaults when none, never a fixed blue", () => {
  assert.deepEqual(r.seriesPalette(theme), ["#0B6E4F", "#F26419", "#33658A"]);
  assert.deepEqual(r.themePalette(theme), theme.palette);
  assert.deepEqual(r.themePalette({ dataColors: ["#111111", "#222222"] }), ["#FFFFFF", "#000000", "#111111", "#222222"]);
  assert.equal(r.seriesPalette(undefined)[0], "#118DFF");
  assert.equal(JSON.stringify(r.DEFAULT_DATA_COLORS).includes("477fbc"), false);
});

test("a mark's color follows entry, series color, default, then the theme by series index", () => {
  const scopes = [{ refs: ["Orders.Region"], value: "East" }];
  const context = { scopes, measureRefs: ["Orders.Sales"], seriesIndex: 1, categoryIndex: 1 };
  const series = { defaultColor: "#1E90FF", seriesColors: { "Orders.Sales": "#0B6E4F" }, categoryColors: [{ value: "East", color: "#cccccc", column: "Orders.Region" }] };
  assert.equal(r.markColor(series, theme, context), "#cccccc");
  assert.equal(r.markColor(series, theme, { ...context, scopes: [{ refs: ["Orders.Region"], value: "West" }] }), "#0B6E4F");
  assert.equal(r.markColor({ ...series, seriesColors: {} }, theme, { ...context, scopes: [{ refs: ["Orders.Region"], value: "West" }] }), "#1E90FF");
  assert.equal(r.markColor({ defaultColor: null }, theme, context), "#F26419");
  assert.equal(r.markColor(undefined, theme, { ...context, seriesIndex: 5 }), "#33658A");
  // The theme palette repeats after its last color.
  assert.equal(r.markColor(null, theme, { ...context, seriesIndex: 3 }), "#0B6E4F");
});

test("category color entries respect their column, measure and value type", () => {
  const entry = (extra) => ({ categoryColors: [{ value: "East", color: "#cccccc", ...extra }] });
  const at = (value, refs = ["Orders.Region"]) => ({ scopes: [{ refs, value }], measureRefs: ["Orders.Sales"], seriesIndex: 0, categoryIndex: 0 });
  assert.equal(r.markColor(entry({ column: "Orders.Segment" }), theme, at("East")), "#0B6E4F", "another column's entry does not apply");
  assert.equal(r.markColor(entry({ metadata: "Orders.Profit" }), theme, at("East")), "#0B6E4F", "another measure's entry does not apply");
  assert.equal(r.markColor(entry({ metadata: "Orders.Sales" }), theme, at("East")), "#cccccc");
  assert.equal(r.markColor(entry({}), theme, at("East")), "#cccccc", "no column means either scope");
  assert.equal(r.markColor({ categoryColors: [{ value: 2019, color: "#cccccc" }] }, theme, at(2019)), "#cccccc");
  assert.equal(r.markColor({ categoryColors: [{ value: 2019, color: "#cccccc" }] }, theme, at("2019")), "#cccccc");
  assert.equal(r.markColor({ categoryColors: [{ value: "2019-01-01T00:00:00", type: "datetime", color: "#cccccc" }] }, theme, at("2019-01-01")), "#cccccc");
  assert.equal(r.markColor({ categoryColors: [{ value: "2019-01-01T00:00:00", type: "datetime", color: "#cccccc" }] }, theme, at("2019-01-02")), "#0B6E4F");
  assert.equal(r.markColor({ categoryColors: [{ value: "East", color: "javascript:x" }] }, theme, at("East")), "#0B6E4F", "an unsafe color is ignored");
});

test("vary by category takes the next theme color per category unless an entry colors it", () => {
  const series = { varyByCategory: true, categoryColors: [{ value: "East", color: "#cccccc" }] };
  const at = (value, categoryIndex) => ({ scopes: [{ refs: [], value }], measureRefs: [], seriesIndex: 0, categoryIndex });
  assert.equal(r.markColor(series, theme, at("West", 0)), "#0B6E4F");
  assert.equal(r.markColor(series, theme, at("East", 1)), "#cccccc");
  assert.equal(r.markColor(series, theme, at("North", 2)), "#33658A");
});

test("dates compare by wall-clock time whatever their spelling", () => {
  assert.equal(r.dateNumber("2019-01-01"), r.dateNumber("2019-01-01T00:00:00"));
  assert.equal(r.dateNumber("2019-01-01 00:00:00"), r.dateNumber("2019-01-01T00:00:00"));
  assert.ok(Number.isNaN(r.dateNumber("January")));
});

// --- chart spec --------------------------------------------------------------------------------------

test("a single series draws the theme's first data color and never the old fixed blue", () => {
  const built = r.buildChart(chart(), theme);
  assert.equal(built.spec.mark.color, "#0B6E4F");
  assert.equal(JSON.stringify(built.spec).toLowerCase().includes("477fbc"), false);
  assert.equal(built.spec.encoding.color, undefined);
  assert.equal(built.spec.data.values.length, 3);
  assert.equal(r.buildChart(chart(), undefined).spec.mark.color, "#118DFF");
  assert.equal(r.buildChart(chart({ formatting: { ...chart().formatting, series: { defaultColor: "#1E90FF" } } }), theme).spec.mark.color, "#1E90FF");
});

test("chart points keep the raw category for clicks and the server-formatted text for tooltips", () => {
  const points = r.planChart(chart(), theme).points;
  assert.deepEqual(points.map(p => [p.category, p.categoryLabel, p.value, p.formatted, p.series, p.color]), [
    ["West", "West", 5, "5", "Sales", "#0B6E4F"], ["East", "East", 12, "12", "Sales", "#0B6E4F"], ["North", "North", 8, "8", "Sales", "#0B6E4F"],
  ]);
});

test("a category color entry switches the chart to per-mark colors", () => {
  const formatting = { ...chart().formatting, series: { categoryColors: [{ value: "East", color: "#cc0000", column: "Orders.Region" }] } };
  const built = r.buildChart(chart({ formatting }), theme);
  assert.deepEqual(built.spec.encoding.color, { field: "color", type: "nominal", scale: null, legend: null });
  assert.deepEqual(built.spec.data.values.map(p => p.color), ["#0B6E4F", "#cc0000", "#0B6E4F"]);
  // The legacy key still colors marks for an engine that sends no token.
  const legacy = r.buildChart(chart({ formatting: { categoryColors: [{ value: "West", color: "#123456" }] } }), theme);
  assert.deepEqual(legacy.spec.data.values.map(p => p.color), ["#123456", "#0B6E4F", "#0B6E4F"]);
});

test("a series field gives one color per series, a legend from the token and clustered offsets", () => {
  const built = r.buildChart(withSeries(), theme);
  assert.deepEqual(built.spec.encoding.color.scale, { domain: ["A", "B"], range: ["#0B6E4F", "#F26419"] });
  assert.equal(built.spec.encoding.color.legend.orient, "top");
  assert.deepEqual(built.spec.encoding.xOffset, { field: "series", type: "nominal", sort: null });
  assert.equal(built.spec.encoding.y.stack, null);
  const hidden = r.buildChart(withSeries({ formatting: { ...withSeries().formatting, legend: { show: false } } }), theme);
  assert.equal(hidden.spec.encoding.color.legend, null);
});

test("a series color entry colors that series by its field", () => {
  const formatting = { ...withSeries().formatting, series: { categoryColors: [{ value: "B", color: "#abcdef", column: "Orders.Segment" }] } };
  const built = r.buildChart(withSeries({ formatting }), theme);
  assert.deepEqual(built.spec.data.values.map(p => p.color), ["#0B6E4F", "#abcdef", "#0B6E4F", "#abcdef"]);
});

test("vary colors by category gives each bar its own theme color, single series or clustered", () => {
  const vary = { ...chart().formatting, series: { varyByCategory: true } };
  const single = r.buildChart(chart({ formatting: vary }), theme);
  assert.deepEqual(single.spec.data.values.map(p => p.color), ["#0B6E4F", "#F26419", "#33658A"]);
  assert.deepEqual(single.spec.encoding.color, { field: "color", type: "nominal", scale: null, legend: null });
  assert.equal(single.spec.mark.color, undefined, "no single fill over the per-bar colors");
  const bar = r.buildChart(chart({ kind: "bar", formatting: vary }), theme);
  assert.deepEqual(bar.spec.encoding.color, { field: "color", type: "nominal", scale: null, legend: null });
  const clustered = r.buildChart(withSeries({ formatting: vary }), theme);
  assert.deepEqual(clustered.spec.data.values.map(p => p.color), ["#0B6E4F", "#0B6E4F", "#F26419", "#F26419"]);
  assert.deepEqual(clustered.spec.encoding.xOffset, { field: "series", type: "nominal", sort: null }, "series bars keep their own slots");
  // A line has one stroke: it keeps the first color.
  const line = r.buildChart(chart({ kind: "line", formatting: { ...vary, chart: { stacking: "none" } } }), theme);
  assert.equal(line.spec.mark.color, "#0B6E4F");
  assert.equal(line.spec.encoding.color, undefined);
});

test("a clustered multi-series chart with a matching category color keeps one slot per series", () => {
  const formatting = { ...withSeries().formatting, series: { categoryColors: [{ value: "East", color: "#cccccc", column: "Orders.Region" }] } };
  const column = r.buildChart(withSeries({ formatting }), theme);
  assert.deepEqual(column.spec.encoding.color, { field: "color", type: "nominal", scale: null, legend: null });
  assert.deepEqual(column.spec.encoding.xOffset, { field: "series", type: "nominal", sort: null });
  const bar = r.buildChart(withSeries({ kind: "bar", formatting }), theme);
  assert.deepEqual(bar.spec.encoding.yOffset, { field: "series", type: "nominal", sort: null });
  assert.equal(bar.spec.encoding.xOffset, undefined);
  const legacy = r.buildChart(withSeries({ formatting: { ...withSeries().formatting, series: undefined, categoryColors: [{ value: "East", color: "#cccccc" }] } }), theme);
  assert.deepEqual(legacy.spec.encoding.xOffset, { field: "series", type: "nominal", sort: null });
  // A single-series chart needs no offset.
  const one = r.buildChart(chart({ formatting: { ...chart().formatting, series: { categoryColors: [{ value: "East", color: "#cccccc", column: "Orders.Region" }] } } }), theme);
  assert.equal(one.spec.encoding.xOffset, undefined);
});

test("stacked bars are stacked here, first series at the bottom, negatives downward", () => {
  const points = [
    { category: "West", series: "A", value: 5 }, { category: "West", series: "B", value: 7 },
    { category: "East", series: "B", value: -3 }, { category: "East", series: "A", value: 2 },
  ];
  const stacked = r.stackSegments(points, ["A", "B"]).map(p => [p.category, p.series, p.start, p.end, p.mid]);
  assert.deepEqual(stacked, [["West", "A", 0, 5, 2.5], ["East", "A", 0, 2, 1], ["West", "B", 5, 12, 8.5], ["East", "B", 0, -3, -1.5]]);
  const formatting = { ...withSeries().formatting, chart: { stacking: "stacked" } };
  const built = r.buildChart(withSeries({ formatting }), theme);
  assert.deepEqual(built.spec.encoding.y, { field: "start", type: "quantitative", stack: null, axis: built.spec.encoding.y.axis });
  assert.deepEqual(built.spec.encoding.y2, { field: "end" });
  assert.equal(built.spec.encoding.xOffset, undefined);
});

test("data labels add a text layer with the token's font, color and position", () => {
  const formatting = { ...chart().formatting, labels: { show: true, fontSize: 14, color: "#ff0000", family: '"Segoe UI"', position: "InsideEnd", displayUnits: 1, decimals: 1 } };
  const built = r.buildChart(chart({ formatting }), theme);
  const [bars, text] = built.spec.layer;
  assert.equal(bars.mark.type, "bar");
  assert.deepEqual(text.mark, { type: "text", color: "#ff0000", fontSize: 14, font: '"Segoe UI"', baseline: "top", dy: 4 });
  assert.deepEqual(text.encoding.color, { value: "#ff0000" });
  assert.deepEqual(built.spec.data.values.map(p => p.label), ["5.0", "12.0", "8.0"]);
  assert.equal(built.spec.padding, undefined, "inside labels need no extra room");
  const outside = r.buildChart(chart({ formatting: { ...formatting, labels: { show: true } } }), theme);
  assert.equal(outside.spec.layer[1].mark.baseline, "bottom");
  assert.ok(outside.spec.padding.top > 8);
});

test("axis tokens become axis settings: hidden axes, titles, fonts, gridlines", () => {
  const axes = {
    category: { show: true, title: true, titleText: "Where", titleColor: "#111111", titleFontSize: 12, fontSize: 11, color: "#222222", family: '"Segoe UI"', gridlines: false },
    value: { show: false, title: false, gridlines: true, gridlineColor: "#eeeeee" },
  };
  const built = r.buildChart(chart({ formatting: { ...chart().formatting, axes } }), theme);
  assert.deepEqual(built.spec.encoding.x.axis, { title: "Where", titleColor: "#111111", titleFontSize: 12, titleFont: '"Segoe UI"', labelColor: "#222222", labelFontSize: 11, labelFont: '"Segoe UI"', grid: false });
  assert.deepEqual(built.spec.encoding.y.axis, { title: null, labels: false, ticks: false, domain: false, grid: true, gridColor: "#eeeeee" });
});

test("horizontal bars swap the channels; lines and areas take the stroke and marker tokens", () => {
  const bar = r.buildChart(chart({ kind: "bar" }), theme);
  assert.equal(bar.spec.encoding.y.field, "categoryLabel");
  assert.equal(bar.spec.encoding.x.field, "value");
  const line = r.buildChart(chart({ kind: "line", formatting: { ...chart().formatting, chart: { stacking: "none" }, line: { strokeWidth: 4, markers: false } } }), theme);
  assert.equal(line.spec.mark.strokeWidth, 4);
  assert.equal(line.spec.mark.point, false);
  assert.equal(line.spec.encoding.y.stack, null);
  const area = r.buildChart(chart({ kind: "area", formatting: { ...chart().formatting, chart: { stacking: "none" } } }), theme);
  assert.deepEqual(area.spec.mark.line, { color: "#0B6E4F" });
});

test("pie and donut vary colors by category from the report's theme", () => {
  const pie = r.buildChart(chart({ kind: "pie" }), theme);
  assert.deepEqual(pie.spec.encoding.color.scale, { domain: ["West", "East", "North"], range: ["#0B6E4F", "#F26419", "#33658A"] });
  assert.deepEqual(pie.spec.encoding.theta, { field: "value", type: "quantitative", stack: true });
  assert.equal(pie.spec.layer[0].mark.innerRadius, undefined);
  const donut = r.buildChart(chart({ kind: "donut" }), theme);
  assert.deepEqual(donut.spec.layer[0].mark.innerRadius, { expr: "min(width, height) / 2 * 0.55" });
});

test("a chart without a category or a numeric measure has no spec (the canvas draws its rows)", () => {
  assert.equal(r.buildChart(chart({ columns: [column("measure", "Sales", "Y")], rows: [[5]] }), theme), null);
  assert.equal(r.buildChart(chart({ rows: [] }), theme), null);
});

test("data label text: display units, decimals and percent formats", () => {
  assert.equal(r.formatDataLabel(1234567, "1,234,567", { displayUnits: 1000000, decimals: 1 }), "1.2M");
  assert.equal(r.formatDataLabel(45231, "45,231", { displayUnits: 1000 }), "45.2K");
  assert.equal(r.formatDataLabel(3, "3", { decimals: 2 }), "3.00");
  assert.equal(r.formatDataLabel(0.256, "25.6%", { decimals: 0 }, "0.0%"), "26%");
  assert.equal(r.formatDataLabel(0.256, "25.6%", { displayUnits: 1000 }, "0.0%"), "25.6%");
  assert.equal(r.formatDataLabel(7, "7 units", {}), "7 units");
  assert.equal(r.formatDataLabel(7, "7 units", undefined), "7 units");
});

test("mergeConfig keeps default settings the caller does not name", () => {
  const merged = mergeConfig({ font: "A", axis: { labelColor: "#111", gridColor: "#222" }, view: { stroke: null } }, { axis: { labelColor: "#333" }, font: "B" });
  assert.deepEqual(merged, { font: "B", axis: { labelColor: "#333", gridColor: "#222" }, view: { stroke: null } });
  assert.deepEqual(mergeConfig({ a: 1 }, undefined), { a: 1 });
});

// --- stacking, hidden, hit testing -------------------------------------------------------------------

const rect = (id, x, y, width, height, rank, extra = {}) => ({ id, x, y, width, height, rank, hidden: false, kind: "card", parentGroup: null, ...extra });

test("rows draw by rank, not by arrival or raw z", () => {
  const rows = [{ id: "a", rank: 3 }, { id: "b", rank: 1 }, { id: "c", rank: 2 }, { id: "d" }];
  assert.deepEqual(r.sortByRank(rows).map(v => v.id), ["b", "c", "a", "d"]);
});

test("hidden visuals are skipped in view mode and ghosts in edit mode", () => {
  assert.equal(r.drawState(false, "view"), "draw");
  assert.equal(r.drawState(true, "view"), "skip");
  assert.equal(r.drawState(true, "edit"), "ghost");
  assert.equal(r.drawState(undefined, "edit"), "draw");
});

test("rects come from the payload in rank order with layout as the fallback and overrides on top", () => {
  const layout = [
    { id: "a", title: "A", x: 0, y: 0, width: 100, height: 100 },
    { id: "b", title: "B", x: 50, y: 50, width: 100, height: 100 },
    { id: "late", title: "Late", x: 10, y: 10, width: 10, height: 10, z: 5 },
  ];
  const rows = [
    chart({ id: "b", rank: 1, position: { x: 60, y: 60, width: 100, height: 100 } }),
    chart({ id: "a", rank: 2, position: { x: 0, y: 0, width: 100, height: 100 }, hidden: true, parent_group: "g" }),
    chart({ id: "g", rank: 3, container_kind: "group", kind: "group", position: { x: 0, y: 0, width: 200, height: 200 } }),
  ];
  const rects = r.layoutRects(layout, rows, { b: { x: 1, y: 2, width: 3, height: 4 } });
  assert.deepEqual(rects.map(x => [x.id, x.rank, x.hidden, x.kind, x.parentGroup]), [["b", 1, false, "column", null], ["a", 2, true, "column", "g"], ["g", 3, false, "group", null], ["late", 4, false, "unknown", null]]);
  assert.deepEqual([rects[0].x, rects[0].y, rects[0].width, rects[0].height], [1, 2, 3, 4]);
  assert.deepEqual([rects[3].x, rects[3].width], [10, 10]);
});

test("selection works before the payload has loaded, from the imported layout", () => {
  const layout = [
    { id: "a", title: "A", x: 0, y: 0, width: 100, height: 100 },
    { id: "b", title: "B", x: 50, y: 50, width: 100, height: 100 },
    { id: "hid", title: "Hidden", x: 0, y: 0, width: 300, height: 300, hidden: true },
  ];
  const rects = r.layoutRects(layout, []);
  assert.deepEqual(rects.map(x => [x.id, x.rank]), [["a", 1], ["b", 2], ["hid", 3]]);
  assert.equal(r.hitTest(rects, { x: 60, y: 60 }), "b");
  assert.equal(r.hitTest(rects, { x: 10, y: 10 }), "a");
  assert.equal(r.hitTest(rects, { x: 250, y: 250 }), null, "a hidden visual is never picked");
});

test("a click picks the topmost visible visual by rank, groups are skipped, empty canvas clears", () => {
  const rects = [
    rect("bottom", 0, 0, 200, 200, 1),
    rect("group", 0, 0, 200, 200, 4, { kind: "group" }),
    rect("member", 50, 50, 100, 100, 2, { parentGroup: "group" }),
    rect("top", 80, 80, 40, 40, 3),
    rect("ghost", 0, 0, 300, 300, 9, { hidden: true }),
  ];
  assert.equal(r.hitTest(rects, { x: 90, y: 90 }), "top");
  assert.equal(r.hitTest(rects, { x: 60, y: 60 }), "member", "group members select individually");
  assert.equal(r.hitTest(rects, { x: 10, y: 10 }), "bottom");
  assert.equal(r.hitTest(rects, { x: 250, y: 250 }), null);
  assert.equal(r.hitTest(rects, { x: 200, y: 10 }), null, "the right edge is outside");
  assert.equal(r.hitTest(rects, { x: 0, y: 0 }), "bottom", "the left and top edges are inside");
});

test("Alt-click cycles to the next visual underneath and wraps to the top", () => {
  const rects = [rect("c", 0, 0, 100, 100, 1), rect("b", 0, 0, 100, 100, 2), rect("a", 0, 0, 100, 100, 3)];
  const at = { x: 10, y: 10 };
  assert.equal(r.hitTest(rects, at, { altKey: true, selectedId: null }), "a");
  assert.equal(r.hitTest(rects, at, { altKey: true, selectedId: "a" }), "b");
  assert.equal(r.hitTest(rects, at, { altKey: true, selectedId: "b" }), "c");
  assert.equal(r.hitTest(rects, at, { altKey: true, selectedId: "c" }), "a");
  assert.equal(r.hitTest(rects, at, { altKey: true, selectedId: "elsewhere" }), "a");
  assert.equal(r.hitTest(rects, at, { selectedId: "b" }), "a", "a plain click still takes the topmost");
  assert.deepEqual(r.hitCandidates(rects, at).map(x => x.id), ["a", "b", "c"]);
});

test("equal ranks fall back to arrival order, later on top", () => {
  const rects = [rect("first", 0, 0, 10, 10, 1), rect("second", 0, 0, 10, 10, 1)];
  assert.equal(r.hitTest(rects, { x: 1, y: 1 }), "second");
});

test("client coordinates convert to page pixels from the measured canvas box", () => {
  assert.deepEqual(r.clientToPage({ left: 100, top: 50, width: 640 }, { width: 1280 }, 0.5, 420, 250), { x: 640, y: 400 });
  assert.deepEqual(r.clientToPage({ left: 0, top: 0, width: 0 }, { width: 1280 }, 0.5, 10, 20), { x: 20, y: 40 }, "falls back to the zoom setting");
});

// --- revisions and merging ---------------------------------------------------------------------------

test("a response is stale only when it echoes a revision that is not the current head", () => {
  assert.equal(r.isStaleResponse("r0002", "r0003"), true);
  assert.equal(r.isStaleResponse("r0003", "r0003"), false);
  assert.equal(r.isStaleResponse(null, "r0003"), false, "an engine that does not echo cannot be judged");
  assert.equal(r.isStaleResponse(undefined, "r0003"), false);
  assert.equal(r.isStaleResponse("r0002", undefined), false, "no head, nothing to compare");
});

test("in-flight requests share a key only for the same path, body and head", () => {
  assert.equal(r.pendingKey("/p", "{}", "r0001"), r.pendingKey("/p", "{}", "r0001"));
  assert.notEqual(r.pendingKey("/p", "{}", "r0001"), r.pendingKey("/p", "{}", "r0002"));
  assert.notEqual(r.pendingKey("/p", "{}"), r.pendingKey("/p", "{}", "r0002"));
});

test("head_moved is recognised from the code, the status or the message", () => {
  assert.equal(r.isHeadMoved(Object.assign(new Error("x"), { code: "head_moved" })), true);
  assert.equal(r.isHeadMoved(new Error("head_moved")), true);
  assert.equal(r.isHeadMoved(Object.assign(new Error("The report changed since you opened it"), { status: 409 })), true);
  assert.equal(r.isHeadMoved(Object.assign(new Error("Power BI Desktop has unsaved changes"), { status: 409 })), false);
  assert.equal(r.isHeadMoved(new Error("The visuals could not load.")), false);
  assert.equal(r.isHeadMoved(null), false);
});

const cachedPage = () => ({
  visuals: [
    chart({ id: "a", rank: 1, data_key: "k1", title: "Old" }),
    chart({ id: "b", rank: 2, data_key: "k2", error: "This page filter needs Power BI Desktop", needs_desktop: true, rows: [], columns: [] }),
    chart({ id: "c", rank: 3, data_key: "k3" }),
  ],
  page_style: { color: "#ffffff" }, theme, revision: "r0001", head_hash: "h1",
});
const formattingRow = (extra = {}) => ({ title: "New", kind: "column", position: { x: 1, y: 2, width: 3, height: 4 }, rank: 1, hidden: false, fidelity: "exact", unrendered: [], data_key: "k1", columns: chart().columns, formatting: { title: { show: false } }, ...extra });

test("formatting rows replace formatting and keep cached data when the data key is unchanged", () => {
  const page = cachedPage();
  const { result, refetch } = r.mergeFormatting(page, {
    visuals: { a: formattingRow({ rank: 3 }), b: formattingRow({ data_key: "k2", columns: [], rank: 1, error: "This page filter needs Power BI Desktop", needs_desktop: true }), c: formattingRow({ data_key: "k3", rank: 2 }) },
    page_style: { color: "#000000" }, theme: { ...theme, name: "Changed" }, revision: "r0002", head_hash: "h2",
  });
  assert.deepEqual(refetch, []);
  assert.deepEqual(result.visuals.map(v => v.id), ["b", "c", "a"], "ranks from the response reorder the page");
  const a = result.visuals.find(v => v.id === "a");
  assert.equal(a.title, "New");
  assert.deepEqual(a.formatting, { title: { show: false } });
  assert.deepEqual(a.rows, page.visuals[0].rows, "cached rows survive");
  assert.deepEqual(a.formatted_rows, page.visuals[0].formatted_rows);
  assert.deepEqual(a.position, { x: 1, y: 2, width: 3, height: 4 });
  assert.equal(a.id, "a");
  assert.deepEqual(result.page_style, { color: "#000000" });
  assert.equal(result.theme.name, "Changed");
  assert.equal(result.revision, "r0002");
  assert.equal(result.head_hash, "h2");
  assert.equal(page.visuals[0].title, "Old", "the cache is not mutated");
});

test("a moved data key, a changed column count or a new visual asks for a refetch instead of merging", () => {
  const { refetch } = r.mergeFormatting(cachedPage(), { visuals: {
    a: formattingRow({ data_key: "changed" }),
    b: formattingRow({ data_key: "k2", columns: chart().columns }),
    c: formattingRow({ data_key: "k3", columns: [...chart().columns, column("measure", "Extra", "Y")] }),
    fresh: formattingRow({ data_key: "k9" }),
  } });
  assert.deepEqual(refetch, ["a", "b", "c", "fresh"]);
});

test("a whole-page response drops deleted visuals", () => {
  const whole = r.mergeFormatting(cachedPage(), { visuals: { a: formattingRow({ data_key: "k1" }), c: formattingRow({ data_key: "k3", rank: 2 }) } });
  assert.deepEqual(whole.result.visuals.map(v => v.id), ["a", "c"]);
});

test("bring to front: every visual takes its new rank from the answer, so no two tie", () => {
  const page = { ...cachedPage(), visuals: ["A", "B", "C"].map((id, i) => chart({ id, rank: i + 1, data_key: `k${id}`, position: { x: 0, y: 0, width: 10, height: 10 } })) };
  const rows = Object.fromEntries([["A", 3], ["B", 1], ["C", 2]].map(([id, rank]) => [id, formattingRow({ data_key: `k${id}`, rank, position: { x: 0, y: 0, width: 10, height: 10 } })]));
  const { result, refetch } = r.mergeFormatting(page, { visuals: rows, revision: "r0002" });
  assert.deepEqual(refetch, []);
  assert.deepEqual(result.visuals.map(v => [v.id, v.rank]), [["B", 1], ["C", 2], ["A", 3]]);
});

test("a group's move and hide reach its members because the whole page is merged", () => {
  const member = (id, extra = {}) => chart({ id, data_key: `k${id}`, parent_group: "G", position: { x: 5, y: 5, width: 10, height: 10 }, hidden: false, ...extra });
  const page = { ...cachedPage(), visuals: [member("m1"), member("m2")] };
  const moved = { x: 105, y: 205, width: 10, height: 10 };
  const rows = Object.fromEntries(["m1", "m2"].map(id => [id, formattingRow({ data_key: `k${id}`, parent_group: "G", position: moved, hidden: true, hidden_own: false })]));
  const { result } = r.mergeFormatting(page, { visuals: rows });
  assert.deepEqual(result.visuals.map(v => [v.position, v.hidden]), [[moved, true], [moved, true]]);
});

test("the measure-driven card graphic survives a formatting-only merge while its measure is unchanged", () => {
  const card = chart({ id: "k", kind: "card", data_key: "kk", formatting: { imageIndex: 1, image: "data:image/svg+xml;base64,AAAA", cardValue: {} } });
  const merge = row => r.mergeFormatting({ ...cachedPage(), visuals: [card] }, { visuals: { k: formattingRow({ data_key: "kk", kind: "card", formatting: row }) } }).result.visuals[0].formatting;
  assert.deepEqual(merge({ imageIndex: 1, cardValue: { color: "#123456" } }), { imageIndex: 1, image: "data:image/svg+xml;base64,AAAA", cardValue: { color: "#123456" } });
  assert.equal(merge({ imageIndex: 2, cardValue: {} }).image, undefined, "another measure: the old graphic is not carried over");
  assert.equal(merge({ imageIndex: null, cardValue: {} }).image, undefined, "the picture switch is off");
  assert.equal(merge({ imageIndex: 1, image: "data:image/png;base64,BBBB" }).image, "data:image/png;base64,BBBB", "a graphic in the answer wins");
});

test("merging with nothing cached refetches everything", () => {
  const out = r.mergeFormatting(undefined, { visuals: { a: formattingRow(), b: formattingRow() } });
  assert.equal(out.result, undefined);
  assert.deepEqual(out.refetch, ["a", "b"]);
});

test("a cached error stays when the formatting row carries none (the filter that failed is part of the data key)", () => {
  const { result } = r.mergeFormatting(cachedPage(), { visuals: { a: formattingRow(), b: formattingRow({ data_key: "k2", columns: [] }), c: formattingRow({ data_key: "k3" }) } });
  const b = result.visuals.find(v => v.id === "b");
  assert.equal(b.error, "This page filter needs Power BI Desktop");
  assert.equal(b.needs_desktop, true);
});

// --- token -> style ----------------------------------------------------------------------------------

test("the container token becomes the visual's box: background alpha, border, radius, shadow, padding", () => {
  const style = r.containerStyle({ background: "#ff880099", border: { show: true, color: "#123456", width: 2, radius: 8 }, shadow: true, padding: [5, null, null, 7] }, "column", false);
  assert.deepEqual(style, {
    backgroundColor: "#ff880099", border: "2px solid #123456", borderRadius: 8, boxShadow: "0 2px 8px #00000040",
    paddingTop: 5, paddingRight: 12, paddingBottom: 12, paddingLeft: 7,
  });
  const plain = r.containerStyle(container({ background: null }), "column", false);
  assert.equal(plain.backgroundColor, "transparent", "no background means transparent, whatever the page looks like");
  assert.equal(plain.border, "none");
  assert.equal(plain.borderRadius, 0);
  assert.equal(plain.boxShadow, "none");
  assert.equal(r.containerStyle(container({ border: { show: false, color: "#ff0000", width: 3, radius: 9 } }), "column", false).borderRadius, 0, "radius only draws with the border on");
  assert.deepEqual(r.containerStyle(undefined, "column", false), {}, "an older engine sends no token: the stylesheet look applies");
});

test("default padding depends on the kind and on compact boxes", () => {
  assert.deepEqual(r.defaultPadding("column", false), [12, 12, 12, 12]);
  assert.deepEqual(r.defaultPadding("column", true), [3, 6, 3, 6]);
  assert.deepEqual(r.defaultPadding("shape", false), [0, 0, 0, 0]);
  assert.deepEqual(r.defaultPadding("image", true), [0, 0, 0, 0]);
  assert.deepEqual(r.defaultPadding("card", false), [0, 0, 0, 0]);
  assert.deepEqual(r.defaultPadding("textbox", false), [5, 5, 5, 5]);
});

test("titles: the chart title token, the card label, the slicer header and the legacy keys", () => {
  const view = r.titleView(chart({ formatting: { title: { show: true, text: "Total MTD Sales", fontSize: 16, fontColor: "#1F2933", fontFamily: '"Segoe UI Semibold", helvetica', bold: false, italic: true, underline: true, alignment: "center", wrap: true, background: "#eeeeee" } } }));
  assert.equal(view.show, true);
  assert.equal(view.text, "Total MTD Sales");
  assert.deepEqual(view.style, { fontSize: 16, color: "#1F2933", fontFamily: '"Segoe UI Semibold", helvetica', fontWeight: 400, fontStyle: "italic", textAlign: "center", textDecoration: "underline", backgroundColor: "#eeeeee", whiteSpace: "normal" });
  assert.equal(r.titleView(chart({ formatting: { title: { show: false, text: "x" } } })).show, false);
  assert.equal(r.titleView(chart({ formatting: { title: { show: true, text: "", textSource: "dynamic" } } })).text, "Sales by Region", "a measure-driven title falls back to the visual's name");
  const card = r.titleView(chart({ kind: "card", formatting: { cardLabel: { show: true, text: "Agents with Positive Change %", fontSize: 13.33, color: "#52606D", family: '"Segoe UI"' } } }));
  assert.equal(card.text, "Agents with Positive Change %");
  assert.equal(card.show, true);
  assert.equal(card.style.fontSize, 13.33);
  assert.equal(r.titleView(chart({ kind: "card", formatting: { cardLabel: { show: false, text: "x" } } })).show, false);
  const slicer = r.titleView(chart({ kind: "slicer", formatting: { slicer: { header: { show: true, text: "Country", bold: true, fontSize: 16 } } } }));
  assert.deepEqual([slicer.show, slicer.text, slicer.style.fontWeight], [true, "Country", 700]);
  assert.equal(r.titleView(chart({ kind: "slicer", formatting: { slicer: { header: { show: false } } } })).show, false);
  assert.equal(r.titleView({ title: "T", kind: "card", formatting: { showLabel: true, label: "Legacy label" } }).text, "Legacy label");
  assert.equal(r.titleView({ title: "T", kind: "column", formatting: { titleVisible: false } }).show, false);
  assert.equal(r.titleView({ title: "T", kind: "slicer", formatting: {} }).show, true);
});

test("a card or slicer whose container title is switched on draws it as its own row", () => {
  const token = { show: true, text: "Total Sales", fontSize: 14, fontColor: "#111111", bold: true, alignment: "center" };
  const card = chart({ kind: "card", formatting: { title: token, cardLabel: { show: true, text: "Label" } } });
  const view = r.containerTitleView(card);
  assert.deepEqual([view.show, view.text, view.style.fontSize, view.style.color, view.style.fontWeight, view.style.textAlign], [true, "Total Sales", 14, "#111111", 700, "center"]);
  assert.equal(r.titleView(card).text, "Label", "the card's own label is unchanged");
  assert.equal(r.containerTitleView(chart({ kind: "slicer", formatting: { title: { show: true, text: "" } } })).text, "Sales by Region", "no text: the visual's name");
  assert.equal(r.containerTitleView(chart({ kind: "card", formatting: { title: { show: false, text: "x" } } })), null);
  assert.equal(r.containerTitleView(chart({ kind: "card", formatting: {} })), null);
  assert.equal(r.containerTitleView(chart()), null, "a chart's title is the ordinary title row");
});

test("table tokens: fonts, banding, grid lines and row padding", () => {
  const view = r.tableView({
    header: { fontSize: 16, color: "#252423", family: '"Segoe UI Semibold"', background: "#f0f0f0", alignment: "center" },
    values: { fontSize: 14, color: "#000000", family: '"Segoe UI"', secondaryColor: "#333333", banding: { primary: "#ffffff", secondary: "#f6f6f6" } },
    grid: { horizontal: true, vertical: false, horizontalColor: "#dddddd", verticalColor: null, rowPadding: 9 },
  }, undefined);
  assert.equal(view.horizontal, "#dddddd");
  assert.equal(view.vertical, undefined);
  assert.equal(view.rowPadding, 9);
  assert.deepEqual(view.header, { fontSize: 16, color: "#252423", fontFamily: '"Segoe UI Semibold"', fontWeight: undefined, fontStyle: undefined, textAlign: "center", backgroundColor: "#f0f0f0" });
  assert.equal(view.cell.fontSize, 14, "the font size does not depend on the grid");
  assert.deepEqual(view.banding, { primary: "#ffffff", secondary: "#f6f6f6", secondaryColor: "#333333" });
  assert.equal(r.tableView({ grid: { horizontal: false } }, undefined).horizontal, undefined);
  assert.equal(r.tableView(undefined, false).horizontal, undefined, "the legacy key still switches the grid off");
  assert.equal(r.tableView(undefined, undefined).horizontal, "#eef1f5");
});

test("card tiles: fill, outline transparency and per-side padding with measured defaults", () => {
  const style = r.tileStyle({ fill: "#f1f2f280", fillShown: true, outline: { show: true, color: "#e6e6e6", width: 1, transparency: 50 }, padding: [null, null, 9, 13] }, r.CARD_TILE_PADDING);
  assert.deepEqual(style, { backgroundColor: "#f1f2f280", border: "1px solid #e6e6e680", paddingTop: 24, paddingRight: 24, paddingBottom: 9, paddingLeft: 13 });
  assert.equal(r.tileStyle({ fill: "#ff0000", fillShown: false }, [0, 0, 0, 0]).backgroundColor, "transparent");
  assert.equal(r.tileStyle(null, [1, 2, 3, 4]).border, "none");
  assert.equal(r.cardValueStyle({ fontSize: 45.33, color: "#1F2933", family: '"Segoe UI Bold"', bold: false, alignment: "left" }, 20).fontSize, 45.33);
  assert.equal(r.cardValueStyle(undefined, 20).fontSize, 20, "the legacy size is the fallback");
});

test("tile slicers: state styles, corner shapes and the grid", () => {
  const formatting = {
    tile: { default: { fill: "#ffffff", fillShown: true, outline: { show: true, color: "#dedede", width: 1, transparency: 0 }, padding: [8, 8, 8, 8] }, selected: { fill: "#0B6E4F", fillShown: true } },
    slicer: { values: { default: { color: "#BCBEC0", fontSize: 16 }, selected: { color: "#ffffff", bold: true } }, layout: { rows: 2, columns: 3, cellPadding: 8, shape: "rectangleRoundedByPixel", radius: 20 } },
  };
  const normal = r.slicerTileStyle(formatting, false);
  assert.equal(normal.backgroundColor, "#ffffff");
  assert.equal(normal.color, "#BCBEC0");
  assert.equal(normal.borderRadius, 20);
  const chosen = r.slicerTileStyle(formatting, true);
  assert.equal(chosen.backgroundColor, "#0B6E4F");
  assert.equal(chosen.color, "#ffffff");
  assert.equal(chosen.fontWeight, 700);
  assert.deepEqual(r.slicerGridStyle(formatting), { display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8 });
  assert.equal(r.slicerTileStyle({ slicerStyle: "tiles" }, false), undefined);
  assert.equal(r.slicerGridStyle({}), undefined);
});

test("text runs become style objects and paragraphs keep their alignment", () => {
  assert.deepEqual(r.runStyle({ text: "Agents", fontFamily: '"Segoe UI Semibold"', fontSize: 24, color: null, bold: true, italic: false, underline: false, hasLink: false }),
    { fontFamily: '"Segoe UI Semibold"', fontSize: 24, color: undefined, fontWeight: 700, fontStyle: undefined, textDecoration: undefined });
  assert.equal(r.runStyle({ text: "link", hasLink: true }).textDecoration, "underline");
  assert.equal(r.runStyle({ text: "x", color: "expression(alert(1))" }).color, undefined);
  assert.equal(r.paragraphStyle({ align: "center", runs: [] }).textAlign, "center");
  assert.equal(r.paragraphStyle({ align: "javascript", runs: [] }).textAlign, undefined);
});

test("shapes draw rectangle, rounded, oval, pill and line; rotation and outline follow the token", () => {
  const box = { width: 200, height: 100 };
  assert.deepEqual(r.shapeStyle({ shape: "oval", fill: "#ff0000ff", outline: { show: true, color: "#000000", width: 3 }, rotation: 0 }, box),
    { position: "absolute", inset: 0, backgroundColor: "#ff0000ff", border: "3px solid #000000", borderRadius: "50%", transform: undefined });
  assert.equal(r.shapeStyle({ shape: "rectangleRounded", radius: 12 }, box).borderRadius, 12);
  assert.equal(r.shapeStyle({ shape: "rectangleRoundedByPixel" }, box).borderRadius, 8);
  assert.equal(r.shapeStyle({ shape: "pill" }, box).borderRadius, 200);
  assert.equal(r.shapeStyle({ shape: "rectangle", radius: 12 }, box).borderRadius, 0);
  assert.equal(r.shapeStyle({ shape: "rectangle", rotation: 45 }, box).transform, "rotate(45deg)");
  assert.equal(r.shapeStyle({ shape: "rectangle", fill: null }, box).backgroundColor, "transparent");
  const line = r.shapeStyle({ shape: "line", outline: { show: true, color: "#123456", width: 4 } }, box);
  assert.equal(line.borderTop, "4px solid #123456");
  assert.equal(line.height, 0);
  assert.equal(r.pictureFit("fill"), "cover");
  assert.equal(r.pictureFit("fit"), "contain");
  assert.equal(r.pictureFit("normal"), "none");
});

test("the page paints wallpaper, then its color over it, then the image", () => {
  const layers = r.pageLayers({ color: "#ffffff", alpha: 0, wallpaper: { color: "#eeeeee", alpha: 1 }, image: "data:image/png;base64,AAAA", scaling: "Fill" });
  assert.deepEqual(layers.map(l => l.key), ["wallpaper", "page", "image"]);
  assert.equal(layers[0].style.backgroundColor, "#eeeeeeff");
  assert.equal(layers[1].style.backgroundColor, "#ffffff00", "a transparent page lets the wallpaper show");
  assert.equal(layers[2].style.backgroundSize, "cover");
  assert.equal(layers[2].style.backgroundImage, 'url("data:image/png;base64,AAAA")');
  assert.equal(r.pageLayers({ color: "#ffffff", image: "data:image/png;base64,AAAA", scaling: "Fit" })[2 - 1].style.backgroundSize, "contain");
  assert.deepEqual(r.pageLayers({ color: "#ffffff", image: "https://example.com/a.png" }).map(l => l.key), ["page"], "a web address is never loaded");
  assert.deepEqual(r.pageLayers(undefined), []);
});

// --- fidelity ----------------------------------------------------------------------------------------

test("unrendered properties are described in plain words", () => {
  assert.equal(r.describeUnrendered(["categoryAxis.innerPadding", "plotArea.transparency"]), "category axis: inner padding, plot area: transparency");
  assert.equal(r.describeUnrendered(["spacing.verticalSpacing"]), "spacing: vertical spacing");
  assert.equal(r.describeUnrendered(Array.from({ length: 10 }, (_, i) => `a.b${i}`)).endsWith("and 2 more"), true);
});

test("fidelity notes: approximate, Power BI only, and a page filter that needs Desktop", () => {
  assert.deepEqual(r.fidelityNote(chart()), { level: "approximate", label: "Some formatting is shown in Power BI only", detail: "category axis: inner padding" });
  assert.equal(r.fidelityNote(chart({ fidelity: "exact", unrendered: [] })), null);
  const gauge = r.fidelityNote({ kind: "unsupported", fidelity: "none", unrendered: ["axis.min"], unsupported_reason: "Gauge visuals are previewed in Power BI only", error: "Gauge visuals are previewed in Power BI only" });
  assert.deepEqual(gauge, { level: "none", label: "Preview in Power BI", detail: "Gauge visuals are previewed in Power BI only" });
  const desktop = r.fidelityNote({ kind: "column", fidelity: "none", error: "This page filter needs Power BI Desktop", needs_desktop: true });
  assert.equal(desktop.level, "desktop");
  assert.equal(desktop.label, "This page filter needs Power BI Desktop");
  assert.equal(r.fidelityNote({ kind: "column", fidelity: "approximate", unrendered: [] }).detail, "Part of this visual's formatting is not drawn here.");
});

// --- one render: head-aware requests, stale drops, formatting-only ---------------------------------

function harness(responses, options = {}) {
  const calls = [];
  const events = [];
  const run = {
    post: async (path, body, headId) => {
      calls.push({ path, body: JSON.parse(body), headId });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    path: "/render", formattingPath: "/render-formatting", requestBody: { selections: [], searches: {} }, headId: "r0002",
    head: () => "r0002", current: () => cachedPage(), formattingOnly: false, cancelled: () => false,
    onResult: result => events.push(["result", result]), onError: reason => events.push(["error", reason.message]), onMoved: () => events.push(["moved"]),
    ...options,
  };
  return { run, calls, events };
}
const fresh = (extra = {}) => ({ visuals: [chart({ id: "a" })], revision: "r0002", head_hash: "h2", ...extra });

test("a full render sends the head as its revision query parameter and hands over the result", async () => {
  const h = harness([fresh()]);
  await r.runRender(h.run);
  assert.deepEqual(h.calls, [{ path: "/render?revision=r0002", body: { selections: [], searches: {} }, headId: "r0002" }]);
  assert.deepEqual(h.events.map(e => e[0]), ["result"]);
  assert.equal(h.events[0][1].revision, "r0002");
});

test("without a head no revision is sent", async () => {
  const h = harness([{ visuals: [], revision: null }], { headId: undefined, head: () => undefined });
  await r.runRender(h.run);
  assert.deepEqual(h.calls, [{ path: "/render", body: { selections: [], searches: {} }, headId: undefined }]);
  assert.deepEqual(h.events.map(e => e[0]), ["result"]);
});

test("a response that echoes another revision is dropped and the head is reported as moved", async () => {
  const h = harness([fresh({ revision: "r0001" })]);
  await r.runRender(h.run);
  assert.deepEqual(h.events, [["moved"]]);
});

test("the head is judged when the response arrives, not when the request left", async () => {
  let head = "r0002";
  const h = harness([fresh()], { head: () => head });
  const started = r.runRender({ ...h.run, post: async (...args) => { const result = await h.run.post(...args); head = "r0003"; return result; } });
  await started;
  assert.deepEqual(h.events, [["moved"]]);
});

test("HTTP 409 head_moved is not an error: the report is refreshed instead", async () => {
  const h = harness([Object.assign(new Error("head_moved"), { code: "head_moved", status: 409 })]);
  await r.runRender(h.run);
  assert.deepEqual(h.events, [["moved"]]);
});

test("other failures reach the error callback with their message", async () => {
  const h = harness([new Error("Power BI Desktop needs the working copy")]);
  await r.runRender(h.run);
  assert.deepEqual(h.events, [["error", "Power BI Desktop needs the working copy"]]);
});

test("a cancelled render reports nothing, whatever arrives", async () => {
  const ok = harness([fresh()], { cancelled: () => true });
  await r.runRender(ok.run);
  const bad = harness([new Error("late")], { cancelled: () => true });
  await r.runRender(bad.run);
  const stale = harness([fresh({ revision: "r0001" })], { cancelled: () => true });
  await r.runRender(stale.run);
  assert.deepEqual([ok.events, bad.events, stale.events], [[], [], []]);
});

test("formatting-only asks the formatting route for the whole page and merges without a data fetch", async () => {
  const response = { visuals: { a: formattingRow({ rank: 1, data_key: "k1" }), b: formattingRow({ rank: 2, data_key: "k2", columns: [] }), c: formattingRow({ rank: 3, data_key: "k3", title: "Renamed" }) }, revision: "r0002", head_hash: "h2" };
  const h = harness([response], { formattingOnly: true });
  await r.runRender(h.run);
  assert.equal(h.calls.length, 1);
  assert.deepEqual(h.calls[0], { path: "/render-formatting?revision=r0002", body: { selections: [], searches: {} }, headId: "r0002" });
  assert.deepEqual(h.events.map(e => e[0]), ["result"]);
  const merged = h.events[0][1];
  assert.equal(merged.visuals.find(v => v.id === "a").title, "New");
  assert.deepEqual(merged.visuals.find(v => v.id === "a").rows, cachedPage().visuals[0].rows);
  assert.equal(merged.visuals.find(v => v.id === "c").title, "Renamed", "a row the edit did not name is refreshed too");
});

test("formatting-only never sends `only`, however many edits arrive back to back", async () => {
  for (const ids of [["a"], ["b"], ["a", "b"]]) {
    const h = harness([{ visuals: { a: formattingRow({ data_key: "k1" }), b: formattingRow({ data_key: "k2", columns: [] }), c: formattingRow({ data_key: "k3" }) }, revision: "r0002" }], { formattingOnly: true, only: ids });
    await r.runRender(h.run);
    assert.deepEqual(h.calls.map(c => c.path), ["/render-formatting?revision=r0002"]);
  }
});

test("formatting-only falls back to a full render when a data key moved", async () => {
  const h = harness([{ visuals: { a: formattingRow({ data_key: "moved" }) }, revision: "r0002" }, fresh()], { formattingOnly: true });
  await r.runRender(h.run);
  assert.deepEqual(h.calls.map(c => c.path), ["/render-formatting?revision=r0002", "/render?revision=r0002"]);
  assert.deepEqual(h.events.map(e => e[0]), ["result"]);
  assert.equal(h.events[0][1].head_hash, "h2");
});

test("formatting-only falls back to a full render when the route fails, and reports a moved head without one", async () => {
  const failing = harness([new Error("Local sidecar returned 404"), fresh()], { formattingOnly: true });
  await r.runRender(failing.run);
  assert.deepEqual(failing.calls.map(c => c.path), ["/render-formatting?revision=r0002", "/render?revision=r0002"]);
  assert.deepEqual(failing.events.map(e => e[0]), ["result"]);
  const moved = harness([Object.assign(new Error("x"), { code: "head_moved" })], { formattingOnly: true });
  await r.runRender(moved.run);
  assert.deepEqual([moved.calls.length, moved.events], [1, [["moved"]]]);
  const stale = harness([{ visuals: { a: formattingRow() }, revision: "r0001" }], { formattingOnly: true });
  await r.runRender(stale.run);
  assert.deepEqual([stale.calls.length, stale.events], [1, [["moved"]]]);
});

test("formatting-only with nothing cached draws the page from a full render", async () => {
  const h = harness([{ visuals: { a: formattingRow() }, revision: "r0002" }, fresh()], { formattingOnly: true, current: () => undefined });
  await r.runRender(h.run);
  assert.deepEqual(h.calls.map(c => c.path), ["/render-formatting?revision=r0002", "/render?revision=r0002"]);
});

test("query parameters are encoded, and each touched id is its own `only`", () => {
  assert.equal(r.withQuery("/p", []), "/p");
  assert.equal(r.withQuery("/p", [["revision", "r0007"], ["only", "a b"], ["only", "c&d"]]), "/p?revision=r0007&only=a%20b&only=c%26d");
});

test("the sidecar's own head_moved sentence is recognised from the message alone", () => {
  assert.equal(r.isHeadMoved(new Error("The report changed while this view was loading. Studio is showing the latest version.")), true);
});
