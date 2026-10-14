import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// Every invocation has its own state; never reuse the user's development engine.
const qaRoot = process.env.STUDIO_RELEASE_QA_ROOT ||= mkdtempSync(join(tmpdir(), "powerbi-release-"));
const repository = resolve("../..");
const python = join(repository, ".venv", "Scripts", "python.exe");
const token = "isolated-playwright-release-token";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  outputDir: join(qaRoot, "browser-results"),
  reporter: [["list"], ["json", { outputFile: join(qaRoot, "browser-results.json") }]],
  use: {
    baseURL: "http://127.0.0.1:15173",
    channel: "msedge",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command: `"${python}" -m ai_powerbi_analyst.studio --host 127.0.0.1 --port 18765 --token ${token} --dev-browser-origin http://127.0.0.1:15173 --repository "${repository}" --state-root "${join(qaRoot, "state")}"`,
      cwd: repository,
      url: "http://127.0.0.1:18765/v1/ping",
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: "npm run dev -- --port 15173",
      url: "http://127.0.0.1:15173",
      reuseExistingServer: false,
      env: { VITE_STUDIO_API_URL: "http://127.0.0.1:18765", VITE_STUDIO_TOKEN: token },
    },
  ],
});
