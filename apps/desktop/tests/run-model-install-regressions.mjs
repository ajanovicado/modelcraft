// Browser plugin not available: use Playwright with an isolated, fixture-only Vite server.
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { stripVTControlCharacters } from "node:util";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const vite = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
const url = "http://127.0.0.1:15178";
const server = spawn(process.execPath, [vite, "--host", "127.0.0.1", "--port", "15178", "--strictPort"], {
  cwd: desktop,
  env: { ...process.env, VITE_STUDIO_API_URL: "http://127.0.0.1:19880", VITE_STUDIO_TOKEN: "fixture-only-model-setup" },
  stdio: ["ignore", "pipe", "pipe"],
});
let output = "";
server.stdout.on("data", chunk => { output += chunk; });
server.stderr.on("data", chunk => { output += chunk; });
server.on("error", error => { output += String(error); });
try {
  let ready = false;
  const deadline = Date.now() + 30_000;
  while (!ready && Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(`Isolated model-setup frontend failed:\n${output}`);
    try { ready = stripVTControlCharacters(output).includes(url) && (await fetch(url)).ok; } catch { /* Wait for this exact port. */ }
    if (!ready) await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!ready) throw new Error(`Isolated model-setup frontend did not become ready:\n${output}`);
  const test = spawn(process.execPath, ["tests/model-install.browser.mjs"], {
    cwd: desktop, env: { ...process.env, STUDIO_TEST_URL: url }, stdio: "inherit",
  });
  const [code, signal] = await once(test, "exit");
  if (signal) throw new Error(`Model-setup regression stopped: ${signal}`);
  process.exitCode = code ?? 1;
} finally {
  if (server.exitCode === null) server.kill();
}
