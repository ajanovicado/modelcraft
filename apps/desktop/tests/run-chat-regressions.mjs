// Run the real frontend against fixture-only API responses on an isolated server.
// A strict port prevents accidentally testing the user's running application.
import { spawn } from "node:child_process";
import { once } from "node:events";
import { fileURLToPath } from "node:url";
import { stripVTControlCharacters } from "node:util";

const desktop = fileURLToPath(new URL("../", import.meta.url));
const vite = fileURLToPath(new URL("../node_modules/vite/bin/vite.js", import.meta.url));
const url = "http://127.0.0.1:15174";
const server = spawn(process.execPath, [vite, "--host", "127.0.0.1", "--port", "15174", "--strictPort"], {
  cwd: desktop,
  env: { ...process.env, VITE_STUDIO_API_URL: "http://127.0.0.1:19876", VITE_STUDIO_TOKEN: "fixture-only-chat-regression" },
  stdio: ["ignore", "pipe", "pipe"],
});
let serverOutput = "";
server.stdout.on("data", chunk => { serverOutput += chunk; });
server.stderr.on("data", chunk => { serverOutput += chunk; });
server.on("error", error => { serverOutput += String(error); });

try {
  const deadline = Date.now() + 30_000;
  let ready = false;
  while (!ready && Date.now() < deadline) {
    if (server.exitCode !== null) throw new Error(`Isolated frontend failed to start:\n${serverOutput}`);
    try { ready = stripVTControlCharacters(serverOutput).includes(url) && (await fetch(url)).ok; } catch { /* Wait for Vite to bind its isolated port. */ }
    if (!ready) await new Promise(resolve => setTimeout(resolve, 200));
  }
  if (!ready) throw new Error(`Isolated frontend did not become ready:\n${serverOutput}`);
  for (const script of ["tests/preview-chart-csp.browser.mjs", "tests/designer-forecast.browser.mjs", "tests/conversation-history.browser.mjs", "tests/assistant-handoff.browser.mjs", "tests/conversation-lifecycle.browser.mjs", "tests/dax-lifecycle.browser.mjs"]) {
    const test = spawn(process.execPath, [script], {
      cwd: desktop,
      env: { ...process.env, STUDIO_TEST_URL: url },
      stdio: "inherit",
    });
    const [code, signal] = await once(test, "exit");
    if (signal) throw new Error(`Chat regression test stopped: ${signal}`);
    if (code !== 0) { process.exitCode = code ?? 1; break; }
  }
} finally {
  if (server.exitCode === null) server.kill();
}
