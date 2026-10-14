import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

// Renders the canvas to static markup: what the tokens, ranks, hidden rules and edit hooks put in the DOM.
const require = createRequire(import.meta.url);
const compiled = await build({
  entryPoints: [new URL("../src/InteractiveReportPage.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")],
  bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" },
  // api.ts reads import.meta.env, which a CommonJS bundle leaves empty: nothing here calls the API.
  logLevel: "silent",
});
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(require, loaded, loaded.exports);
const { OriginalCanvas } = loaded.exports;
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

const noop = () => undefined;
const theme = { dataColors: ["#0B6E4F", "#F26419"], foreground: "#252423", background: "#FFFFFF" };
const container = (extra = {}) => ({ background: null, border: { show: false, color: null, width: null, radius: 0 }, shadow: false, padding: null, ...extra });
const row = (id, kind, rank, extra = {}) => ({
  id, title: `${id} title`, kind, visual_type: kind, container_kind: "visual", position: { x: 10 * rank, y: 10, width: 200, height: 100 },
  z: rank * 1000, rank, hidden: false, hidden_own: false, parent_group: null, group_children: [], fidelity: "exact", unrendered: [],
  columns: [], rows: [], selected: [], truncated: false, formatting: { title: { show: true, text: `${id} title` }, container: container() }, ...extra,
});
const layoutOf = (rows) => ({ width: 1280, height: 720, visuals: rows.map(v => ({ id: v.id, title: v.title, ...v.position })) });
const render = (rows, props = {}) => renderToStaticMarkup(React.createElement(OriginalCanvas, {
  layout: layoutOf(rows), pageStyle: { color: "#ffffff", alpha: 1 }, theme, visuals: rows, mode: "view", onSelect: noop, onSearch: noop,
  disabled: false, resetKey: 0, loading: false, canReset: false, onRefresh: noop, onReset: noop, ...props,
}));
const ids = (html) => [...html.matchAll(/data-visual-id="([^"]+)"/g)].map(m => m[1]);
const zIndexOf = (html, id) => Number(new RegExp(`data-visual-id="${id}"[^>]*style="[^"]*z-index:(\\d+)`).exec(html)?.[1]);
const textbox = (id, rank, text, extra = {}) => row(id, "textbox", rank, { formatting: { title: { show: false, text: "" }, container: container(), content: [{ align: "left", runs: [{ text }] }] }, ...extra });

test("visuals are drawn in rank order with z-index equal to the rank, never the raw z", () => {
  const html = render([textbox("c", 3, "c"), textbox("a", 1, "a"), textbox("b", 2, "b")]);
  assert.deepEqual(ids(html), ["a", "b", "c"]);
  assert.deepEqual(["a", "b", "c"].map(id => zIndexOf(html, id)), [1, 2, 3]);
  assert.equal(html.includes("z-index:1000"), false);
});

test("no overlay layer without an overlay prop", () => {
  assert.equal(render([textbox("a", 1, "a")]).includes("report-overlay-layer"), false);
});

test("hidden visuals are not drawn in view mode and are ghosts with a Hidden caption in edit mode", () => {
  const rows = [textbox("shown", 1, "x"), textbox("gone", 2, "y", { hidden: true, hidden_own: true })];
  assert.deepEqual(ids(render(rows)), ["shown"]);
  const edit = render(rows, { mode: "edit" });
  assert.deepEqual(ids(edit), ["shown", "gone"]);
  assert.match(edit, /class="ghost-caption">Hidden</);
  assert.match(edit, /aria-label="gone title \(hidden\)"/);
  assert.match(edit, /data-visual-id="gone"[^>]*opacity:0\.35/);
  assert.equal((edit.match(/ghost-caption/g) ?? []).length, 1);
});

test("a group draws no article and no content; its members are drawn on the page; a group background is a plain rectangle", () => {
  const rows = [
    row("grp", "group", 1, { container_kind: "group", group_children: ["kid"], formatting: { container: container(), titleVisible: false } }),
    textbox("kid", 2, "member", { parent_group: "grp" }),
  ];
  const html = render(rows);
  assert.deepEqual(ids(html), ["kid"]);
  assert.equal(html.includes("needs a dedicated local renderer"), false);
  assert.equal(html.includes("report-group-box"), false);
  const filled = render([{ ...rows[0], formatting: { container: container({ background: "#ff0000ff" }), titleVisible: false } }, rows[1]]);
  assert.match(filled, /class="report-group-box" aria-hidden="true" style="[^"]*z-index:1;background-color:#ff0000ff/);
  assert.deepEqual(ids(filled), ["kid"]);
  // A hidden group hides its members through the cascaded flag and its own box in view mode.
  const hidden = rows.map(v => ({ ...v, hidden: true }));
  assert.deepEqual(ids(render(hidden)), []);
  assert.deepEqual(ids(render(hidden, { mode: "edit" })), ["kid"]);
});

test("the container token sets the box; a null background is transparent even when the page has an image", () => {
  const styled = textbox("a", 1, "x", { formatting: { title: { show: false, text: "" }, container: container({ background: "#ff880099", border: { show: true, color: "#123456", width: 2, radius: 8 }, shadow: true, padding: [5, null, null, 7] }), content: [] } });
  const html = render([styled]);
  assert.match(html, /background-color:#ff880099/);
  assert.match(html, /border:2px solid #123456/);
  assert.match(html, /border-radius:8px/);
  assert.match(html, /padding-top:5px/);
  assert.match(html, /padding-left:7px/);
  const withImage = render([textbox("b", 1, "x")], { pageStyle: { color: "#ffffff", alpha: 1, image: "data:image/png;base64,AAAA", scaling: "Fill" } });
  assert.match(withImage, /data-visual-id="b"[^>]*background-color:transparent/);
  assert.match(withImage, /background-size:cover/);
  assert.equal(withImage.includes("legacy-visual"), false);
});

test("a row without a container token keeps the legacy look", () => {
  const html = render([{ ...textbox("a", 1, "x"), formatting: { showValue: true } }]);
  assert.match(html, /original-live-visual legacy-visual/);
});

test("the page paints its wallpaper and color as layers behind the visuals", () => {
  const html = render([textbox("a", 1, "x")], { pageStyle: { color: "#ffffff", alpha: 0, wallpaper: { color: "#eeeeee", alpha: 1 } } });
  assert.match(html, /class="report-page-layer" aria-hidden="true" style="background-color:#eeeeeeff"/);
  assert.match(html, /class="report-page-layer" aria-hidden="true" style="background-color:#ffffff00"/);
  assert.ok(html.indexOf("report-page-layer") < html.indexOf("data-visual-id"));
});

test("text runs are text nodes: markup in a payload string is escaped, styles come from validated tokens", () => {
  const evil = textbox("a", 1, "<img src=x onerror=alert(1)>", { formatting: { title: { show: false, text: "" }, container: container(), content: [{ align: "center", runs: [{ text: "<img src=x onerror=alert(1)>", color: "red; background:url(x)", fontFamily: "Arial}body{x:y", fontSize: 24, bold: true }] }] } });
  const html = render([evil]);
  assert.match(html, /&lt;img src=x onerror=alert\(1\)&gt;/);
  assert.equal(html.includes("<img"), false);
  assert.match(html, /<span style="font-size:24px;font-weight:700">/);
  assert.match(html, /<p style="text-align:center;/);
});

test("Power BI only visuals are a calm frame with their reason, not an error box", () => {
  const gauge = row("gauge", "unsupported", 1, { fidelity: "none", unsupported_reason: "Gauge visuals are previewed in Power BI only", error: "Gauge visuals are previewed in Power BI only", unrendered: ["axis.min"] });
  const html = render([gauge]);
  assert.match(html, /class="report-frame report-frame-none" role="note"><strong>gauge title<\/strong><span>Gauge visuals are previewed in Power BI only<\/span><em>Preview in Power BI<\/em>/);
  assert.equal(html.includes("original-live-message"), false);
  assert.equal(html.includes("fidelity-badge"), false);
});

test("a visual that needs Desktop keeps its box and says so", () => {
  const html = render([row("chart", "column", 1, { fidelity: "none", error: "This page filter needs Power BI Desktop", needs_desktop: true })]);
  assert.match(html, /class="report-frame report-frame-desktop" role="note"><strong>This page filter needs Power BI Desktop<\/strong>/);
  assert.match(html, /left:10px;top:10px;width:200px;height:100px/);
});

test("a picture the file sets but Studio cannot read is an empty frame, not a broken image", () => {
  const missing = row("pic", "image", 1, { fidelity: "approximate", unrendered: ["general.imageUrl"], formatting: { title: { show: false, text: "" }, container: container(), picture: { src: null, scaling: "fill" } } });
  assert.match(render([missing]), /class="report-picture-empty" role="img"/);
  const good = { ...missing, formatting: { ...missing.formatting, picture: { src: "data:image/png;base64,iVBORw0KGgo=", scaling: "fit" } } };
  assert.match(render([good]), /<img class="report-picture" src="data:image\/png;base64,iVBORw0KGgo=" alt="pic title" style="object-fit:contain"\/>/);
  const web = { ...missing, formatting: { ...missing.formatting, picture: { src: "https://evil.example/x.png", scaling: "fit" } } };
  assert.equal(render([web]).includes("evil.example"), false);
});

test("edit mode: an application region, a listbox of options with aria-selected and tab stops; fidelity badge only here", () => {
  const rows = [row("a", "column", 1, { fidelity: "approximate", unrendered: ["categoryAxis.innerPadding"] }), textbox("b", 2, "x")];
  const edit = render(rows, { mode: "edit", selectedVisualId: "b" });
  assert.match(edit, /role="application" aria-label="Report canvas, editing\. Tab moves between visuals and Enter selects one\." tabindex="0"/);
  assert.match(edit, /class="report-visuals" role="listbox" aria-multiselectable="true" aria-label="Visuals on this page"/);
  assert.equal((edit.match(/class="original-live-content" inert=""/g) ?? []).length, 2, "the bodies (selects, sort buttons) are neither clickable nor tab stops");
  assert.equal(edit.includes("original-live-title\" title=\"Inspect"), false, "no button nested inside an option");
  assert.equal(render(rows, { selectedVisualId: "b" }).includes("inert"), false);
  assert.match(edit, /<div class="original-live-visual[^"]*" aria-label="a title" data-visual-id="a"[^>]*role="option" aria-selected="false" tabindex="0"/);
  assert.match(edit, /data-visual-id="b"[^>]*role="option" aria-selected="true" tabindex="0"/);
  assert.match(edit, /class="fidelity-badge" role="note" title="Some formatting is shown in Power BI only\. category axis: inner padding"/);
  assert.match(edit, /original-live-canvas editing/);
  const view = render(rows, { selectedVisualId: "b" });
  assert.match(view, /role="region" aria-label="Original report canvas" tabindex="0"/);
  assert.match(view, /<article class="original-live-visual[^"]*" aria-label="a title"/);
  assert.equal(view.includes("listbox"), false);
  assert.equal(view.includes("fidelity-badge"), false);
  assert.equal(view.includes("aria-selected"), false);
});

test("a card or slicer with its container title switched on draws that title above its tile", () => {
  const card = row("k", "card", 1, {
    columns: [{ kind: "measure", table: "T", name: "Sales", label: "Sales", role: "Values" }], rows: [[5]],
    formatting: { title: { show: true, text: "Total Sales", fontSize: 14 }, container: container(), cardLabel: { show: true, text: "Label" }, cardValue: {}, tile: { default: null } },
  });
  const html = render([card]);
  assert.match(html, /<div class="original-live-container-title" style="[^"]*font-size:14px[^"]*">Total Sales<\/div>/);
  assert.match(html, /original-live-title[^>]*>Label</, "the card label is still drawn");
  const off = render([{ ...card, formatting: { ...card.formatting, title: { show: false, text: "Total Sales" } } }]);
  assert.equal(off.includes("original-live-container-title"), false);
});

test("the overlay is rendered inside the canvas above the visuals with the scale, page and rects", () => {
  let seen;
  const rows = [textbox("a", 1, "a"), textbox("b", 2, "b", { hidden: true })];
  const html = render(rows, { mode: "edit", layoutOverride: { a: { x: 500, y: 60, width: 30, height: 40 } }, overlay: (context) => { seen = context; return React.createElement("i", { id: "frame" }); } });
  assert.match(html, /class="report-overlay-layer"><i id="frame"><\/i><\/div>/);
  assert.ok(html.indexOf("data-visual-id") < html.indexOf("report-overlay-layer"));
  assert.equal(seen.page.width, 1280);
  assert.equal(seen.page.height, 720);
  assert.equal(typeof seen.scale, "number");
  assert.equal(typeof seen.toPage, "function");
  assert.deepEqual(seen.rects.map(r => [r.id, r.x, r.y, r.width, r.height, r.rank, r.hidden, r.kind, r.parentGroup]), [
    ["a", 500, 60, 30, 40, 1, false, "textbox", null], ["b", 20, 10, 200, 100, 2, true, "textbox", null],
  ]);
  assert.match(html, /data-visual-id="a"[^>]*left:500px;top:60px;width:30px;height:40px/, "an override moves the visual without a refetch");
});

test("before the render arrives every layout box is drawn and selectable", () => {
  const layout = { width: 1280, height: 720, visuals: [{ id: "a", title: "First", x: 0, y: 0, width: 100, height: 50 }, { id: "b", title: "Second", x: 10, y: 10, width: 100, height: 50 }] };
  const html = render([], { layout, mode: "edit", disabled: true });
  assert.deepEqual(ids(html), ["a", "b"]);
  assert.match(html, /Loading…/);
  assert.deepEqual([zIndexOf(html, "a"), zIndexOf(html, "b")], [1, 2]);
});

test("the inspect button keeps its accessible name and pressed state in view mode", () => {
  const html = render([textbox("a", 1, "x")], { selectedVisualId: "a" });
  assert.match(html, /<button class="original-live-title" style="white-space:normal" title="Inspect a title" aria-label="Inspect a title" aria-pressed="true">a title<\/button>/);
});

test("card tiles and titles come from the tokens; a hidden title stays reachable as the inspect control", () => {
  const card = row("card", "card", 1, {
    columns: [{ kind: "measure", name: "Rate", label: "Rate", role: "Data", format: "", alignment: "", colorIndex: null }], rows: [[0.5]], formatted_rows: [["50%"]],
    formatting: {
      cardValue: { show: true, fontSize: 45.33, color: "#1F2933", family: '"Segoe UI Bold"', bold: false, italic: false, alignment: "left" },
      cardLabel: { show: false, text: "Agents", fontSize: 13.33, color: "#52606D" }, container: container(),
      tile: { default: { fill: "#f1f2f280", fillShown: true, outline: { show: true, color: "#e6e6e6", width: 1, transparency: 0 }, padding: [null, null, 9, 13] }, selected: null }, references: [],
    },
  });
  const html = render([card]);
  assert.match(html, /class="report-tile" style="background-color:#f1f2f280;border:1px solid #e6e6e6ff;padding-top:24px;padding-right:24px;padding-bottom:9px;padding-left:13px"/);
  assert.match(html, /<strong style="font-size:45\.33px;color:#1F2933;font-family:&quot;Segoe UI Bold&quot;;font-weight:400;text-align:left" title="0\.5">50%<\/strong>/);
  assert.match(html, /hidden-title/);
  assert.match(html, /class="original-live-title"/);
});

test("tables take fonts, banding, grid lines and column widths from the table token", () => {
  const table = row("table", "table", 1, {
    columns: [{ kind: "column", name: "Agent", label: "Agent", role: "Values", query_ref: "Orders.Agent", format: "", alignment: "", colorIndex: null }, { kind: "measure", name: "Sales", label: "Sales", role: "Values", query_ref: "Orders.Sales", format: "", alignment: "", colorIndex: null }],
    rows: [["Ann", 5], ["Bob", 7]], formatted_rows: [[null, "5"], [null, "7"]],
    formatting: {
      title: { show: false, text: "" }, container: container({ background: "#203040ff" }),
      table: {
        header: { fontSize: 16, color: "#ffffff", family: '"Segoe UI Semibold"', background: null, alignment: null },
        values: { fontSize: 14, color: "#000000", family: '"Segoe UI"', secondaryColor: "#333333", banding: { primary: "#ffffff", secondary: "#f6f6f6" } },
        grid: { horizontal: true, vertical: false, horizontalColor: "#dddddd", verticalColor: null, rowPadding: 9 },
        columns: { "Orders.Sales": { alignment: "center", styleHeader: true, width: 78.92 } },
      },
    },
  });
  const html = render([table]);
  assert.match(html, /<div class="live-table-scroll token-table" style="font-size:14px;color:#000000;font-family:&quot;Segoe UI&quot;"/);
  // The sticky header paints the container's own opaque color over scrolled rows.
  assert.match(html, /<th[^>]*style="font-size:16px;color:#ffffff;font-family:&quot;Segoe UI Semibold&quot;;background-color:#203040"/);
  assert.match(html, /<th[^>]*style="[^"]*text-align:center;background-color:#203040;width:78\.92px;min-width:78\.92px"/);
  assert.match(html, /<tr style="background-color:#ffffff"><td[^>]*>Ann<\/td>/);
  assert.match(html, /<tr style="background-color:#f6f6f6;color:#333333"><td/);
  assert.match(html, /border-top:1px solid #dddddd;border-left:none;padding-top:9px;padding-bottom:9px/);
});

// --- Round 3 (contract sections 12 to 23) -------------------------------------------------------------------

const col = (kind, name, extra = {}) => ({ kind, table: "Orders", name, label: name, role: "Values", query_ref: `Orders.${name}`, format: "", alignment: "", colorIndex: null, hidden: false, ...extra });

test("a table draws the server's conditional colors, icons and data bars, and its totals row", () => {
  const table = row("t", "table", 1, {
    columns: [
      col("column", "Agent", { colorIndex: 3 }),
      col("measure", "Sales", { backgroundIndex: 2, icon: { index: 4, layout: "before", alignment: "middle" } }),
      col("computed", "values.backColor", { hidden: true, computed: "gradient", query_ref: null }),
      col("computed", "values.fontColor", { hidden: true, computed: "rules", query_ref: null }),
      col("computed", "values.icon", { hidden: true, computed: "rules", query_ref: null }),
      col("measure", "Rank", { dataBars: { positive: "#00aa00", negative: "#aa0000", hideText: false, axis: null, reverse: false, min: null, max: null } }),
    ],
    rows: [["Ann", 5, "#000000", "#ff0000", "ColoredArrowUp", 3], ["Bob", 7, null, null, null, 6]], formatted_rows: [[null, "5", null, null, null, "3"], [null, "7", null, null, null, "6"]],
    totals: { values: [null, 12, null, null, null, 9], formatted: [null, "12", null, null, null, "9"] },
    formatting: { title: { show: false, text: "" }, container: container(), table: { total: { show: true, label: "Total", bold: true }, columns: {} } },
  });
  const html = render([table]);
  assert.match(html, /<td style="[^"]*color:#ff0000">Ann<\/td>/, "the rule's font color");
  assert.match(html, /<td class="numeric" style="[^"]*;background-color:#000000"><span class="cf-cell cf-middle"><svg class="report-cf-icon"[^>]*aria-label="colored arrow up"/);
  assert.match(html, /<td class="numeric" style="border-top:none;border-left:none">7<\/td>/, "no rule for this row: plain");
  assert.match(html, /class="live-data-bar-fill" style="left:0%;width:50%;background:#00aa00"/);
  assert.match(html, /<tfoot><tr class="live-table-total" style="[^"]*font-weight:700[^"]*"><td style="[^"]*">Total<\/td><td class="numeric" style="[^"]*">12<\/td><td class="numeric" style="[^"]*">9<\/td><\/tr><\/tfoot>/);
  assert.equal(html.includes("values.backColor"), false, "helper columns are not drawn");
  const off = render([{ ...table, formatting: { ...table.formatting, table: { total: { show: false } } } }]);
  assert.equal(off.includes("<tfoot>"), false);
});

test("buttons draw their default state; a target that no longer exists disables them with a reason", () => {
  const states = { default: { fill: "#112233ff", outline: { show: true, color: "#605e5c", width: 1 }, text: { show: true, text: "Go", fontSize: 16, color: "#ffffff", alignment: "center", verticalAlignment: "middle" }, icon: { show: true, name: "rightArrow", color: "#ffffff" } }, hover: null, selected: null, disabled: { fill: "#cccccc80", outline: { show: false }, text: { show: true, text: "Go", color: "#888888" }, icon: { show: false } } };
  const button = (action) => row("b", "button", 1, { action, formatting: { title: { show: false, text: "" }, container: container(), button: { shape: "rectangle", states } } });
  const live = render([button({ type: "page", target: "p2", valid: true, tooltip: "Next page" })]);
  assert.match(live, /<button type="button" class="report-button" title="Next page" aria-label="Go" style="background-color:#112233ff;border:1px solid #605e5c;border-radius:0;justify-content:center;align-items:center;flex-direction:row"><svg[^>]*lucide-arrow-right/);
  assert.match(live, /<span style="font-size:16px;color:#ffffff;text-align:center">Go<\/span>/);
  const dead = render([button({ type: "page", target: "gone", valid: false })]);
  assert.match(dead, /class="report-button is-disabled" aria-disabled="true" title="The page, bookmark or web address this button points at no longer exists"[^>]*style="background-color:#cccccc80;border:none/);
  const qna = render([button({ type: "qna", target: null, valid: true })]);
  assert.equal(qna.includes("aria-disabled"), false, "a Power BI only action stays enabled");
  assert.equal(render([button({ type: "page", target: "p2", valid: true })]).includes("original-live-title"), false, "no hidden inspect button over the button");
});

test("a navigator draws one button per item and marks the current one", () => {
  const nav = row("n", "navigator", 1, { formatting: { title: { show: false, text: "" }, container: container(),
    button: { shape: "rectangle", states: { default: { fill: "#ffffffff", outline: { show: true, color: "#605e5c", width: 1 }, text: { text: null, fontSize: 14 } }, hover: null, press: null, selected: { fill: "#252423ff", outline: { show: false }, text: { color: "#ffffff" } } }, layout: { orientation: "horizontal" } },
    navigator: { kind: "pages", items: [{ label: "One", action: { type: "page", target: "one", valid: true }, selected: true }, { label: "Four", action: { type: "page", target: "four", valid: true }, selected: false }] } } });
  const html = render([nav]);
  assert.match(html, /<nav class="report-navigator" aria-label="Pages: n title" style="display:grid;grid-template-columns:repeat\(2, minmax\(0, 1fr\)\)/);
  assert.match(html, /<button type="button" class="report-button" aria-current="page" style="background-color:#252423ff;border:none[^"]*"><span style="color:#ffffff">One<\/span>/);
  assert.match(html, /<button type="button" class="report-button" style="background-color:#ffffffff;border:1px solid #605e5c[^"]*"><span style="font-size:14px">Four<\/span>/);
});

test("a shape with an action is a button in view mode and plain in edit mode", () => {
  const shape = row("s", "shape", 1, { action: { type: "bookmark", target: "B1", valid: true, tooltip: "Show the focus view" }, formatting: { title: { show: false, text: "" }, container: container(), shape: { shape: "rectangle", fill: "#ff0000ff" } } });
  assert.match(render([shape], { points: { run: noop } }), /<button type="button" class="report-action-area" aria-label="Show the focus view" title="Show the focus view"><div class="report-shape"/);
  assert.equal(render([shape], { mode: "edit", points: { run: noop } }).includes("report-action-area"), false);
  assert.equal(render([shape]).includes("report-action-area"), false, "without an action handler (a tooltip preview) it is only a shape");
});

test("data notes put an honest badge on the visual in view mode too; a formatting-only approximation does not", () => {
  const noted = row("c", "column", 1, { fidelity: "approximate", unrendered: [], dataNotes: ["Only the first 100 rows were used for the values computed here"] });
  assert.match(render([noted]), /class="fidelity-badge data" role="note" title="Values may differ from Power BI\. Only the first 100 rows were used for the values computed here\."/);
  assert.equal(render([row("d", "column", 1, { fidelity: "approximate", unrendered: ["categoryAxis.innerPadding"] })]).includes("fidelity-badge"), false);
});

test("a measure-driven title and background replace the static ones", () => {
  const html = render([textbox("x", 1, "body", { formatting: { title: { show: true, text: "", textSource: "dynamic" }, container: container({ background: "#ffffffff" }), content: [], dynamicProperties: ["title.text", "background.color"] }, dynamic: { "title.text": "Sales 2026", "background.color": "#ffee00" } })]);
  assert.match(html, />Sales 2026<\/button>/);
  assert.match(html, /data-visual-id="x"[^>]*background-color:#ffee00/);
});

test("a card's value and tile take their conditional colors", () => {
  const card = row("k", "card", 1, {
    columns: [col("measure", "Sales"), col("computed", "value.fontColor", { hidden: true, computed: "rules", query_ref: null }), col("computed", "tile.fill", { hidden: true, computed: "gradient", query_ref: null })],
    rows: [[5, "#ff0000", "#00ff0080"]], formatted_rows: [["5", null, null]],
    formatting: { container: container(), cardValue: { show: true, fontSize: 30, colorIndex: 1 }, cardLabel: { show: true, text: "Sales" }, tile: { default: { fill: "#ffffffff", fillShown: true, fillIndex: 2 }, selected: null } },
  });
  const html = render([card]);
  assert.match(html, /<strong style="font-size:30px;color:#ff0000"[^>]*>5<\/strong>/);
  assert.match(html, /class="report-tile" style="background-color:#00ff0080/);
});

test("small multiples draw one titled chart per panel", () => {
  const visual = row("sm", "column", 1, {
    columns: [col("column", "Year"), col("column", "Region"), col("measure", "Sales")],
    rows: [[2025, "West", 5], [2026, "West", 9]], formatted_rows: [["2025", null, "5"], ["2026", null, "9"]],
    smallMultiples: { index: 0, field: "Year", values: [2025, 2026], panels: [{ value: 2025, rows: [0] }, { value: 2026, rows: [1] }] },
    formatting: { title: { show: true, text: "Sales" }, container: container(), smallMultiples: { index: 0, layout: { rows: null, columns: null } } },
  });
  const html = render([visual]);
  assert.match(html, /class="live-small-multiples" style="grid-template-columns:repeat\(2, minmax\(0, 1fr\)\);grid-template-rows:repeat\(1, minmax\(0, 1fr\)\)"/);
  assert.match(html, /<section class="live-small-multiple" aria-label="sm title: 2025"><h4>2025<\/h4>/);
  assert.equal((html.match(/class="chart" role="img"/g) ?? []).length, 2);
});

test("a matrix with rows is drawn stepped; without rows it keeps its calm frame", () => {
  const matrix = row("m", "matrix", 1, { columns: [col("column", "Region"), col("column", "City"), col("measure", "Sales")], rows: [["West", "LA", 1], ["West", "SF", 2]], formatted_rows: [[null, null, "1"], [null, null, "2"]] });
  const html = render([matrix]);
  assert.match(html, /live-matrix-stepped/);
  assert.match(html, /<tr class="matrix-parent"><td style="padding-left:10px">West<\/td>/);
  assert.match(html, /<td style="padding-left:26px">LA<\/td><td class="numeric">1<\/td>/);
  const framed = render([row("m", "matrix", 1, { fidelity: "none", unsupported_reason: "Matrix subtotals and hierarchy expansion are previewed in Power BI only" })]);
  assert.match(framed, /report-frame report-frame-none/);
});

test("the phone view draws only the visuals placed on the phone layout, on a 320 px canvas, with no overlay", () => {
  const rows = [textbox("a", 1, "first"), textbox("b", 2, "second"), textbox("c", 3, "third")];
  const mobile = { visuals: { c: { x: 0, y: 0, width: 320, height: 100, z: 0 }, a: { x: 0, y: 120, width: 320, height: 200, z: 1 } } };
  const html = render(rows, { mobile, phone: true, overlay: () => React.createElement("i", { id: "frame" }) });
  assert.deepEqual(ids(html), ["c", "a"]);
  assert.match(html, /class="original-live-canvas" style="width:320px;height:320px/);
  assert.match(html, /aria-label="Report canvas, phone layout"/);
  assert.equal(html.includes("report-overlay-layer"), false);
  assert.deepEqual(ids(render(rows, { mobile, phone: false })), ["a", "b", "c"], "the desktop layout is unchanged");
});

test("a chart draws through the report's theme palette, not the studio draft's", () => {
  const chart = row("chart", "column", 1, {
    columns: [{ kind: "column", name: "Region", label: "Region", role: "Category", format: "", alignment: "", colorIndex: null }, { kind: "measure", name: "Sales", label: "Sales", role: "Y", format: "", alignment: "", colorIndex: null }],
    rows: [["West", 5]], formatting: { title: { show: true, text: "Sales by Region" }, container: container() },
  });
  const html = render([chart]);
  assert.match(html, /<div class="live-chart"><div class="chart" role="img" aria-label="chart title"><\/div><\/div>/);
});
