# Contributing

## Development environment

- Python 3.12 with [`uv`](https://docs.astral.sh/uv/)
- Node.js 22
- Rust (stable) for the Tauri host
- Optional but recommended: `pre-commit install` to enable the repo's hooks

Start with the [synthetic demo](docs/demo.md) or [development guide](docs/development.md).

## Required checks

Run the full gate before submitting changes:

```powershell
uv sync --frozen --python 3.12.12 --extra dev
uv run ruff check src tests typings
uv run ruff format --check src tests typings
uv run mypy src/ai_powerbi_analyst
$testRoot = Join-Path $env:TEMP ('pt-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
uv run pytest --basetemp $testRoot

cd apps/desktop
npm ci
npm run lint
npm run typecheck
npm test
npm run build
npx playwright install chromium
npm run test:chat-browser
npm run test:model-browser

cd src-tauri
cargo fmt --check
cargo clippy --locked --all-targets --features source-check -- -D warnings
cargo check --locked --features source-check
```

CI runs the same checks on `windows-latest`, plus `pip-audit` and `npm audit`.

## Development workflow

The desktop app needs two processes:

1. The authenticated sidecar, e.g.
   `ai-powerbi-studio --host 127.0.0.1 --port 8765 --token <dev-token> --dev-browser-origin http://127.0.0.1:5173`
2. The Vite dev server: `npm run dev` in `apps/desktop` (port 5173)

Restart the sidecar after Python changes — Vite hot reload does not reload it.

`npm run test:e2e` is a release-level suite: it requires the sidecar, a qualified
local model/runtime, and Power BI tooling. `scripts/verify-release.ps1` records source
engineering checks. Signing, installed-app acceptance, and real-model/Desktop
qualification remain separate. `source-check` is compilation-only.

## Non-negotiable design rules

- The local model produces bounded proposals. It never receives shell
  access, raw SQL, arbitrary filesystem paths, PBIR JSON, or unrestricted DAX.
- New model-facing output must go through closed Pydantic contracts
  (`extra="forbid"`, `frozen=True`).
- SQL values are always parameterized; identifiers must come from the governed
  catalog and be quoted.
- Mutating API endpoints require idempotency keys and `expected_base_hash`
  optimistic concurrency.
- PBIP writes must go through the stage → validate → atomic promote → undo
  pipeline; never write report files directly.
- Keep `ai-powerbi-studio.exe` (GUI) and `ai-powerbi-studio-engine.exe`
  (sidecar) as distinct executables.

See `docs/project-index.md` for the module map and `docs/architecture.md` for
the full trust-boundary model.
