// Render the real PreviewChart under the desktop script policy. Every API is blocked.
// A nonce permits only this fixture's bootstrap; it never enables eval.
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { chromium } from "playwright";

const origin = new URL(process.env.STUDIO_TEST_URL || "http://127.0.0.1:15174/").origin;
const transformed = await (await fetch(`${origin}/src/PreviewChart.tsx`)).text();
const transformedMain = await (await fetch(`${origin}/src/main.tsx`)).text();
const reactPath = transformed.match(/from\s+["']([^"']*\/react\.js[^"']*)["']/)?.[1];
const clientPath = transformedMain.match(/from\s+["']([^"']*\/react-dom_client\.js[^"']*)["']/)?.[1];
assert.ok(reactPath && clientPath, "Use the frontend's actual React module instances");
const { app: { security: { csp: desktopPolicy } } } = JSON.parse(await readFile(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8"));
const nonce = randomBytes(18).toString("base64");
const csp = desktopPolicy.replace("script-src 'self'", `script-src 'self' 'nonce-${nonce}'`);
assert.notEqual(csp, desktopPolicy, "The fixture bootstrap receives a nonce");
assert.ok(!csp.includes("unsafe-eval"), "The chart must work without weakening expression security");

const values = [{ category: "East", row: 1, value: 490 }, { category: "West", row: 2, value: 1560 }];
const cases = [
  { id: "bar", label: "Total Sales by Region", spec: { data: { values }, mark: "bar", encoding: { x: { field: "category", type: "nominal" }, y: { field: "value", type: "quantitative" } } } },
  { id: "line", label: "Total Sales over time", spec: { data: { values: [{ date: "2025-01-01", value: 100 }, { date: "2025-02-01", value: 200 }, { date: "2025-03-01", value: 150 }] }, mark: { type: "line", point: true }, encoding: { x: { field: "date", type: "temporal" }, y: { field: "value", type: "quantitative" } } } },
  { id: "donut", label: "Sales distribution", spec: { data: { values }, mark: { type: "arc", innerRadius: 55 }, encoding: { theta: { field: "value", type: "quantitative" }, color: { field: "category", type: "nominal" } } } },
];

const harness = `<!doctype html><html><head><meta charset="utf-8"><title>Modelcraft chart CSP fixture</title>
<style>html,body{margin:0;font:14px/1.5 "Segoe UI",sans-serif;color:#172033;background:#f7f8fa}main{padding:24px;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}section{min-width:0;padding:20px;border:1px solid #dfe3e8;border-radius:12px;background:white}h2{margin:0 0 16px;font-size:14px}.chart{width:100%;height:280px}.chart svg{display:block}button{min-height:36px;margin:24px 24px 0;padding:8px 12px;border:1px solid #dfe3e8;border-radius:8px;background:white}#root.narrow main{max-width:560px;grid-template-columns:minmax(0,1fr)}.chart-empty{min-height:280px;display:grid;place-items:center}</style></head><body><div id="root"></div><script type="module" nonce="${nonce}">
import React from ${JSON.stringify(reactPath)};
import ReactDOMClient from ${JSON.stringify(clientPath)};
import RefreshRuntime from "/@react-refresh";
RefreshRuntime.injectIntoGlobalHook(window);
window.$RefreshReg$=()=>{};
window.$RefreshSig$=()=>type=>type;
window.__vite_plugin_react_preamble_installed__=true;
window.fixture={clicks:[],hovers:[],menus:[],violations:[]};
document.addEventListener("securitypolicyviolation",event=>window.fixture.violations.push({directive:event.violatedDirective,blockedURI:event.blockedURI}));
const {PreviewChart}=await import("/src/PreviewChart.tsx");
const cases=${JSON.stringify(cases)};
function Harness(){const [narrow,setNarrow]=React.useState(false);React.useEffect(()=>{document.getElementById("root").classList.toggle("narrow",narrow)},[narrow]);return React.createElement(React.Fragment,null,React.createElement("button",{onClick:()=>setNarrow(value=>!value)},"Resize charts"),React.createElement("main",null,...cases.map(entry=>React.createElement("section",{key:entry.id,"data-chart":entry.id},React.createElement("h2",null,entry.label),React.createElement(PreviewChart,{spec:entry.spec,label:entry.label,...(entry.id==="bar"?{onDatumClick:datum=>window.fixture.clicks.push(datum),onDatumHover:datum=>window.fixture.hovers.push(datum),onDatumContextMenu:datum=>window.fixture.menus.push(datum)}:{})})))))}
ReactDOMClient.createRoot(document.getElementById("root")).render(React.createElement(Harness));
</script></body></html>`;

const browserName = process.env.STUDIO_TEST_BROWSER || "chromium";
const browser = await chromium.launch({ headless: true, ...(browserName === "chromium" ? {} : { channel: browserName }) });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, serviceWorkers: "block" });
page.setDefaultTimeout(15_000);
const errors = [];
const warnings = [];
let apiAttempts = 0;
page.on("pageerror", error => errors.push(String(error)));
page.on("console", message => {
  if (message.type() === "error") errors.push(message.text());
  if (message.type() === "warn") warnings.push(message.text());
});
await page.route(`${origin}/__preview-chart-csp-fixture`, route => route.fulfill({ headers: { "Content-Security-Policy": csp }, contentType: "text/html", body: harness }));
// Native builds have no Vite HMR socket. Disable only development transport here,
// keeping the component, installed chart modules and desktop CSP unchanged.
await page.route(`${origin}/@vite/client`, route => route.fulfill({ contentType: "text/javascript", body: "export const createHotContext=()=>undefined; export const injectQuery=url=>url;" }));
await page.route("**/v1/**", route => { apiAttempts++; return route.abort(); });
const evidence = process.env.STUDIO_TEST_EVIDENCE_DIR;
const capture = async name => {
  if (!evidence) return;
  await mkdir(evidence, { recursive: true });
  await page.screenshot({ path: join(evidence, `${name}.png`), animations: "disabled" });
};

try {
  await page.goto(`${origin}/__preview-chart-csp-fixture`, { waitUntil: "networkidle" });
  assert.equal(await page.title(), "Modelcraft chart CSP fixture");
  for (const entry of cases) {
    const chart = page.locator(`[data-chart="${entry.id}"] .chart`);
    await chart.locator("svg").waitFor();
    assert.equal(await chart.getAttribute("aria-label"), entry.label, "Embedding preserves the chart's meaningful accessible name");
    assert.equal(await page.getByRole("img", { name: entry.label, exact: true }).count(), 1, "The rendered SVG has a discoverable accessible name");
    assert.ok(await chart.locator(".role-mark path").count() > 0, `${entry.id} contains plotted data marks`);
  }
  assert.equal(await page.locator(".chart-empty, vite-error-overlay").count(), 0);
  const bar = page.locator('[data-chart="bar"] .chart');
  assert.match(await bar.textContent(), /1k|1\.5k/, "Custom quantitative label expressions run through the interpreter");
  const mark = bar.locator(".mark-rect.role-mark path").first();
  await mark.hover();
  await page.waitForFunction(() => window.fixture.hovers.some(datum => datum?.category));
  await mark.click();
  await page.waitForFunction(() => window.fixture.clicks.length === 1);
  await mark.click({ button: "right" });
  await page.waitForFunction(() => window.fixture.menus.length === 1);
  const before = await bar.locator("svg").evaluate(svg => svg.getBoundingClientRect().width);
  await capture("preview-chart-strict-csp-desktop");
  await page.getByRole("button", { name: "Resize charts", exact: true }).click();
  await page.waitForFunction(width => document.querySelector('[data-chart="bar"] svg')?.getBoundingClientRect().width < width - 20, before);
  for (const entry of cases) await page.locator(`[data-chart="${entry.id}"] .chart svg`).waitFor();
  assert.equal(await page.locator(".chart-empty").count(), 0);
  await capture("preview-chart-strict-csp-resized");
  await page.setViewportSize({ width: 1040, height: 700 });
  for (const entry of cases) await page.locator(`[data-chart="${entry.id}"] .chart svg`).waitFor();
  const receipt = await page.evaluate(() => ({ ...window.fixture, horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth }));
  assert.deepEqual(receipt.violations, [], "Rendering and interaction must not trigger CSP violations");
  assert.equal(receipt.horizontalOverflow, false);
  assert.equal(apiAttempts, 0, "The regression never reaches the local engine");
  assert.deepEqual(errors, []);
  assert.deepEqual(warnings, []);
  if (evidence) await writeFile(join(evidence, "preview-chart-csp-receipt.json"), JSON.stringify({ status: "passed", browser: browserName, desktopPolicy, fixturePolicy: csp, viewport: page.viewportSize(), ...receipt, errors, warnings, apiAttempts }, null, 2));
  console.log(`PASS (${browserName}): actual PreviewChart renders bar, temporal line and donut under the desktop CSP without unsafe-eval; numeric-label expressions, hover/click/context-menu callbacks and ResizeObserver re-rendering work. No CSP violations, console errors or API calls.`);
} catch (error) {
  await capture("preview-chart-strict-csp-failure");
  console.error(JSON.stringify({ errors, warnings, apiAttempts, fixture: await page.evaluate(() => window.fixture), charts: await page.locator(".chart").evaluateAll(nodes => nodes.map(node => ({ role: node.getAttribute("role"), label: node.getAttribute("aria-label") }))), accessibility: await page.locator("main").ariaSnapshot() }, null, 2));
  throw error;
} finally {
  await browser.close();
}
