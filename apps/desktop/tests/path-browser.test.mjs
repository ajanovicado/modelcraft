import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const compiled = await build({ entryPoints: [new URL("../src/pathBrowser.ts", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1")], bundle: true, platform: "node", format: "cjs", packages: "external", write: false });
const loaded = { exports: {} };
new Function("require", "module", "exports", compiled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const { pathSegments, quickAccessPaths, filterBrowseEntries } = loaded.exports;

const entries = [
  { name: "orders.csv", path: "C:\\data\\orders.csv", kind: "file", selectable: true },
  { name: "regions.xlsx", path: "C:\\data\\regions.xlsx", kind: "file", selectable: true },
  { name: "Orders 2025", path: "C:\\data\\Orders 2025", kind: "dir", selectable: false },
];

test("windows paths become clickable breadcrumbs", () => {
  assert.deepEqual(pathSegments("C:\\Users\\developer\\data"), [
    { name: "C:\\", path: "C:\\" },
    { name: "Users", path: "C:\\Users" },
    { name: "developer", path: "C:\\Users\\developer" },
    { name: "data", path: "C:\\Users\\developer\\data" },
  ]);
});

test("drive roots, trailing separators, UNC and posix paths stay navigable", () => {
  assert.deepEqual(pathSegments("D:\\"), [{ name: "D:\\", path: "D:\\" }]);
  assert.deepEqual(pathSegments("C:\\Users\\developer\\"), pathSegments("C:\\Users\\developer"));
  assert.deepEqual(pathSegments("\\\\server\\share\\data"), [
    { name: "\\\\", path: "\\\\" },
    { name: "server", path: "\\\\server" },
    { name: "share", path: "\\\\server\\share" },
    { name: "data", path: "\\\\server\\share\\data" },
  ]);
  assert.deepEqual(pathSegments("/home/analyst"), [
    { name: "/", path: "/" },
    { name: "home", path: "/home" },
    { name: "analyst", path: "/home/analyst" },
  ]);
  assert.deepEqual(pathSegments("   "), []);
});

test("quick access points at the standard user folders", () => {
  assert.deepEqual(quickAccessPaths("C:\\Users\\developer\\"), [
    { name: "Desktop", path: "C:\\Users\\developer\\Desktop" },
    { name: "Documents", path: "C:\\Users\\developer\\Documents" },
    { name: "Downloads", path: "C:\\Users\\developer\\Downloads" },
  ]);
  assert.deepEqual(quickAccessPaths(""), []);
});

test("search matches names case-insensitively and keeps the engine order", () => {
  assert.deepEqual(filterBrowseEntries(entries, "").map((entry) => entry.name), ["orders.csv", "regions.xlsx", "Orders 2025"]);
  assert.deepEqual(filterBrowseEntries(entries, "  ORDER  ").map((entry) => entry.name), ["orders.csv", "Orders 2025"]);
  assert.deepEqual(filterBrowseEntries(entries, "parquet"), []);
});
