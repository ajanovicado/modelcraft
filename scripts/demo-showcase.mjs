// Browser-only reviewer demo. Both servers bind loopback; all API data is synthetic/in-memory.
import { createServer as createHttpServer } from "node:http";
import { createRequire } from "node:module";
import { join } from "node:path";
import { mkdtemp, rmdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { fileURLToPath, pathToFileURL } from "node:url";
import { injectDemoNotice, showcaseFixture } from "./showcase-fixture.mjs";
import { createShowcaseResponder } from "./showcase-api.mjs";

const root = fileURLToPath(new URL("../", import.meta.url));
const desktop = join(root, "apps", "desktop");
const requireDesktop = createRequire(join(desktop, "package.json"));
const { createServer: createViteServer } = await import(pathToFileURL(requireDesktop.resolve("vite")).href);
const { default: react } = await import(pathToFileURL(requireDesktop.resolve("@vitejs/plugin-react")).href);
const origin = "http://127.0.0.1:15179";
const token = "synthetic-showcase-fixture-only";
const respond = createShowcaseResponder(showcaseFixture());
const envDir = await mkdtemp(join(tmpdir(), "modelcraft-demo-env-"));
let vite;
const api = createHttpServer(async (request, response) => {
  const headers = { "Access-Control-Allow-Origin": origin, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Cache-Control": "no-store" };
  if (request.headers.host !== "127.0.0.1:19881" || request.headers.origin && request.headers.origin !== origin) {
    response.writeHead(403, { "Content-Type": "application/json" }); response.end(JSON.stringify({ detail: "Synthetic demo accepts only its own loopback origin." })); return;
  }
  if (request.method === "OPTIONS") { response.writeHead(204, headers); response.end(); return; }
  if (request.headers.authorization !== `Bearer ${token}`) { response.writeHead(401, { ...headers, "Content-Type": "application/json" }); response.end(JSON.stringify({ detail: "Synthetic demo session required." })); return; }
  let text = "";
  try {
    for await (const chunk of request) {
      text += chunk;
      if (text.length > 64_000) { response.writeHead(413, headers); response.end(); return; }
    }
    const result = respond(request.method || "GET", new URL(request.url || "/", "http://127.0.0.1:19881").pathname, text ? JSON.parse(text) : {});
    response.writeHead(result.status, { ...headers, "Content-Type": result.contentType });
    response.end(result.contentType === "application/json" ? JSON.stringify(result.body) : result.body);
  } catch { response.writeHead(400, { ...headers, "Content-Type": "application/json" }); response.end(JSON.stringify({ detail: "Invalid synthetic demo request." })); }
});
let stopping = false;
const stop = async () => {
  if (stopping) return;
  stopping = true;
  await vite?.close();
  await new Promise(resolve => api.close(resolve));
  await rmdir(envDir);
};
try {
  await new Promise((resolve, reject) => { api.once("error", reject); api.listen(19881, "127.0.0.1", resolve); });
  process.env.VITE_STUDIO_API_URL = "http://127.0.0.1:19881";
  process.env.VITE_STUDIO_TOKEN = token;
  vite = await createViteServer({ root: desktop, configFile: false, envDir, clearScreen: false,
    plugins: [react(), { name: "modelcraft-synthetic-demo-notice", transformIndexHtml: injectDemoNotice }],
    server: { host: "127.0.0.1", port: 15179, strictPort: true, open: false } });
  await vite.listen();
  console.log(`\nModelcraft reviewer demo: ${origin}\nSynthetic data and prepared AI replies only. No model, Python, Rust, database or Power BI process is used.\nExplore Report, Data, Model and DAX Lab. Press Ctrl+C to stop.\n`);
  process.once("SIGINT", () => void stop());
  process.once("SIGTERM", () => void stop());
} catch (error) { await stop(); throw error; }
