// A separate fixture-only port cannot attach to the user's running frontend.
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const vite = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
const url = "http://127.0.0.1:15175";
const server = spawn(process.execPath, [vite, "--host", "127.0.0.1", "--port", "15175", "--strictPort"], { cwd: desktop, env: { ...process.env, VITE_STUDIO_API_URL: "http://127.0.0.1:19877", VITE_STUDIO_TOKEN: "fixture-only-data-connections" }, stdio: ["ignore", "pipe", "pipe"] });
let output = "";
server.stdout.on("data", chunk => { output += chunk; });
server.stderr.on("data", chunk => { output += chunk; });
server.on("error", error => { output += String(error); });
try {
  let ready = false;
  const deadline = Date.now() + 30000;
  while (!ready && Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(`Isolated data-source frontend failed:\n${output}`);
    try { ready = output.includes(url) && (await fetch(url)).ok; } catch { /* Wait for the isolated port. */ }
    if (!ready) await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!ready) throw new Error(`Isolated frontend did not become ready:\n${output}`);
  for (const script of ["tests/data-sources.browser.mjs", "tests/source-entry.browser.mjs", "tests/model-storage.browser.mjs"]) {
    const test = spawn(process.execPath, [script], { cwd: desktop, env: { ...process.env, STUDIO_TEST_URL: url }, stdio: "inherit" });
    const [code, signal] = await once(test, "exit");
    if (signal) throw new Error(`Data-source regression stopped: ${signal}`);
    if (code !== 0) { process.exitCode = code ?? 1; break; }
  }
} finally { if (server.exitCode === null) server.kill(); }
