# Developing Modelcraft

## Synthetic demo

Run `npm ci` and `npm run demo` from `apps/desktop`. This is the real React
frontend with an in-memory synthetic API and fixed assistant examples. It needs
Node.js 22 and no model, Python, Rust, database account or Desktop. See the
[walkthrough](demo.md).

## Full source development

Use Windows 10/11 x64, Python 3.12, uv, and Node.js 22. Native Tauri builds also
need stable Rust and Microsoft C++ Build Tools. Power BI Desktop is external and
required for Desktop operations. The legacy Streamlit demo remains in the code,
while the current desktop UI is React and Tauri.

```powershell
uv sync --frozen --python 3.12.12 --extra dev --extra desktop
cd apps/desktop
npm ci
cd ../..
```

Start an isolated backend from the repository root:

```powershell
$env:MODELCRAFT_DEV_TOKEN = [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N')
uv run --frozen ai-powerbi-studio `
  --host 127.0.0.1 --port 8765 `
  --token $env:MODELCRAFT_DEV_TOKEN `
  --dev-browser-origin http://127.0.0.1:5173 `
  --repository . --state-root .runtime/studio-dev-state
```

In another terminal, use the same token and run:

```powershell
cd apps/desktop
$env:VITE_STUDIO_API_URL = 'http://127.0.0.1:8765'
$env:VITE_STUDIO_TOKEN = '<the token from the backend terminal>'
npm run dev
```

Open `http://127.0.0.1:5173`. Restart the backend after Python changes; Vite
reloads the client independently. Native builds use their own random token and
ephemeral loopback port and do not use these development values.

## Local AI and native tools

The selected model is text-only Qwen3.5 4B Q4_K_M, approximately 3.4 GB. Weights
and machine-specific sealed profiles are excluded from public source. Public
profiles are explicitly unqualified: editing `qualified` or copying a model tag
does not qualify a runtime.

A full AI-enabled build needs verified runtime input, source/build receipts,
the pinned model identity, and the report toolchain. The model is installed in
the user's local data directory on first launch. See [provenance](runtime-provenance.md),
[runtime integration](qwen-text-only-integration-20260921.md), and
[Windows releases](windows-release.md). Historical benchmark paths document
earlier experiments; they are not portable setup instructions.

## Verification

```powershell
uv run --frozen --extra dev ruff check src tests typings
uv run --frozen --extra dev ruff format --check src tests typings
uv run --frozen --extra dev mypy src/ai_powerbi_analyst
$testRoot = Join-Path $env:TEMP ('pt-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
uv run --frozen --extra dev pytest --basetemp $testRoot
cd apps/desktop
npm test
npm run lint
npm run build
npx playwright install chromium
npm run test:chat-browser
npm run test:model-browser
npm run test:data-browser
cd src-tauri
cargo fmt --check
cargo clippy --locked --all-targets --features source-check -- -D warnings
cargo check --locked --features source-check
```

`source-check` compiles the host without a runtime payload. It cannot run the app
or produce a release. Fixture browser tests start dedicated servers and mock
their APIs without opening saved workspaces. The real-model/Power BI suite
(`test:e2e`) needs a qualified runtime and external Desktop tooling.

Use a fresh, short test directory as shown above. Power BI path-length guards
also apply to generated test projects; long temporary paths can trigger them.

The release runner records source hashes, logs and timing and rejects changes
during its run. Signing, installed-app tests, model semantics, native rendering
and recovery are independent [release gates](release-checklist.md).

## Writable data

Installed state lives under `%LOCALAPPDATA%/AI Power BI Analyst`; the legacy
directory name is retained for compatibility. Development uses
`.runtime/studio-dev-state`, excluded from Git. Contributions should use small
synthetic fixtures rather than real data, credentials, workspaces or weights.
