import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { build } from "esbuild";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

const catalog = JSON.parse(await readFile(new URL("../../../src/ai_powerbi_analyst/studio/report_visuals.json", import.meta.url), "utf8"));
const compiled = await build({ entryPoints: [new URL("../src/ForecastNumberInput.tsx", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic" });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { ForecastNumberInput, forecastNumberError } = loaded.exports;

for (const field of ["forecast_length", "ignore_last", "seasonality"]) {
  const { minimum, maximum } = catalog.editing.visual_forecast.settings[field];
  test(`${field} accepts the catalog bounds and rejects incomplete, fractional, and out-of-range edits`, () => {
    assert.equal(forecastNumberError(String(minimum), minimum, maximum), undefined);
    assert.equal(forecastNumberError(String(maximum), minimum, maximum), undefined);
    for (const text of ["", " ", "1.5", String(minimum - 1), String(maximum + 1)]) {
      assert.equal(forecastNumberError(text, minimum, maximum), `Use a whole number from ${minimum} to ${maximum}.`);
    }
    assert.equal(forecastNumberError("12", minimum, maximum), undefined);
  });
}

test("only optional automatic seasonality accepts an empty value", () => {
  assert.equal(forecastNumberError("", 1, 120, true), undefined);
  assert.equal(forecastNumberError("0", 1, 120, true), "Use a whole number from 1 to 120.");
});

const render = overrides => renderToStaticMarkup(createElement(ForecastNumberInput, {
  label: "Forecast length (points)", value: 6, minimum: 1, maximum: 60,
  onValueChange() {}, onValidityChange() {}, ...overrides,
}));

test("invalid numeric values keep their text and expose an accessible recovery message", () => {
  const html = render({ value: 61 });
  assert.match(html, /value="61"/);
  assert.match(html, /aria-invalid="true"/);
  assert.match(html, /aria-label="Forecast length \(points\)"/);
  const describedBy = html.match(/aria-describedby="([^"]+)"/)[1];
  assert.ok(html.includes(`id="${describedBy}"`));
  assert.match(html, /designer-field-error/);
  assert.match(html, /Use a whole number from 1 to 60\./);
  assert.doesNotMatch(render({ value: 12 }), /designer-field-error/);
});

test("automatic seasonality is named explicitly instead of appearing as a missing required value", () => {
  const html = render({ label: "Seasonality (points)", value: null, maximum: 120, allowAuto: true });
  assert.match(html, /placeholder="Auto"/);
  assert.match(html, /aria-invalid="false"/);
  assert.match(html, /Leave blank for automatic seasonality\./);
});
