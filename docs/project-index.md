> Historical local record. Referenced machine artifacts are excluded from this public source snapshot. Use the [README](../README.md) and [development guide](development.md) for current setup and tested scope.

# Project index

Reviewed against local source and running services on 2026-09-04. This is a navigation index for future work, not an assertion that every original release requirement is implemented or qualified.

## Product and runtime

AI Power BI Studio imports selected local files, profiles them, proposes a semantic model and dashboard, accepts reviewed revisions, and builds Power BI projects. The current interface is React/TypeScript with a Tauri Windows host. Browser development uses the same Python Studio API. Streamlit is the older demo interface.

Runtime flow:

`React UI -> authenticated /v1 API -> StudioService -> ingestion / planner / model / aggregates -> reviewed draft -> SemanticProjectBuilder -> validators -> Desktop refresh and render verification`

The model is Qwen `qwen3.5:4b-q4_K_M` served by local Ollama. The pinned digest is `sha256:2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`. Typed proposals are validated before compilation; model output is not directly executed as project JSON or shell commands.

## Where to work

| Concern | Primary files |
| --- | --- |
| Screens, workspace selection, chat, review/build actions | `apps/desktop/src/App.tsx` |
| Page/visual editor, bindings, grid placement | `apps/desktop/src/DashboardDesigner.tsx`, `DashboardDesigner.css` |
| Table relationships, hidden fields, measure catalog and DAX review | `apps/desktop/src/ModelWorkbench.tsx`, `ModelWorkbench.css` |
| Workflow rail, command palette, evidence and shared UI | `apps/desktop/src/ProductUI.tsx` |
| Logical charts and UI styling | `apps/desktop/src/PreviewChart.tsx`, `styles.css` |
| Frontend API/SSE transport and data contracts | `apps/desktop/src/api.ts`, `types.ts` |
| Native window, random loopback port, per-launch token, engine lifecycle | `apps/desktop/src-tauri/src/lib.rs` |
| Native capabilities, installer assets and engine registration | `apps/desktop/src-tauri/capabilities/default.json`, `tauri.conf.json`, `icons/` |
| Python entry point and authenticated HTTP routes | `src/ai_powerbi_analyst/studio/__main__.py`, `api.py` |
| Workflow orchestration and integration | `src/ai_powerbi_analyst/studio/service.py` |
| Strict versioned Studio contracts | `src/ai_powerbi_analyst/studio/contracts.py` |
| SQLite objects, idempotency, events and conversations | `src/ai_powerbi_analyst/studio/state.py` |
| Folder inventory, format signatures, CSV encoding | `src/ai_powerbi_analyst/studio/inventory.py` |
| Original-byte snapshots, normalized Parquet, profiles | `src/ai_powerbi_analyst/studio/ingestion.py`, `data/onboarding.py` |
| Full-snapshot relationship evidence and validation | `src/ai_powerbi_analyst/studio/relationship_engine.py` |
| Typed aggregate queries and query receipts | `src/ai_powerbi_analyst/studio/aggregates.py` |
| Initial drafts, Qwen revisions, DAX proposals, previews, layout and field validation | `src/ai_powerbi_analyst/studio/planner.py` |
| Existing PBIP compatibility scan | `src/ai_powerbi_analyst/studio/compatibility.py` |
| Qwen identity, health, structured generation, streaming and unload | `src/ai_powerbi_analyst/llm/runtime.py` |
| Model/tool settings and discovery | `src/ai_powerbi_analyst/settings.py`, `preflight.py`, `config/defaults.yaml` |
| Power Query M, TMDL, governed DAX, PBIR, source relink and validation | `src/ai_powerbi_analyst/powerbi/semantic_model.py` |
| Advanced DAX validation and calendar generation | `src/ai_powerbi_analyst/powerbi/dax_validation.py`, `calendar_table.py` |
| Visual catalog, PBIR contracts and recipes | `src/ai_powerbi_analyst/powerbi/visual_catalog.py`, `pbir_contract.py`, `recipes.py` |
| Project staging, tree hashes and rollback | `src/ai_powerbi_analyst/powerbi/transaction.py` |
| Real Desktop refresh and screenshots | `src/ai_powerbi_analyst/powerbi/desktop_hydration.py`, `orchestration/desktop.py` |
| Older fixture/demo paths | `src/ai_powerbi_analyst/ui/`, `runtime/`, `scripts/launch-demo.ps1` |

## Studio API groups

- Health and workspace loading: `GET /v1/health`, `GET/POST /v1/workspaces`, workspace detail.
- Data: `POST /v1/inventory`, `POST /v1/ingestions`, snapshot detail.
- Drafts: create/get, revisions, dashboard updates, final approval, per-measure advanced DAX approval.
- Conversation: `POST /v1/chat/stream` (SSE tokens, heartbeat, done/error); workspace messages.
- Projects: compatibility scan; build creation; open, export, relink and undo by build ID.
- Evidence: workspace build history, events and approvals.

Most mutations use request contracts containing idempotency and expected-base-hash fields; inspect each actual route before extending concurrency behavior. SQLite stores JSON objects plus event, conversation and idempotency records.

## State and saved work

- Native default: `%LOCALAPPDATA%\AI Power BI Analyst`.
- Existing browser development workspace: `.runtime/studio-dev-state` (keep this explicit when launching to retain the user's workspace).
- Database: `.runtime/studio-dev-state/studio.sqlite3`.
- Managed data: `sources/`, `snapshots/`, `manifests/` under the state directory.
- Managed outputs, backups and screenshots: `projects/` under the state directory.
- Active workspace observed on 2026-09-04: `Superstore Local`, ID `workspace-84c3b5fdcc6840359f5ca7df3f18113e`.
- Its loaded draft is approved, with 4 pages, 11 visuals and 6 measures.
- Existing report: `.runtime/studio-dev-state/projects/superstore_executive_ai/superstore_executive_ai.pbip`.

## Launch on this machine

First check listeners on 5173, 8765 and 11434 to avoid duplicate processes. Run from the repository root in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m ai_powerbi_analyst.studio --host 127.0.0.1 --port 8765 --token studio-development-token-local-only --dev-browser-origin http://127.0.0.1:5173 --repository . --state-root .runtime\studio-dev-state
```

Run the frontend in a separate terminal:

```powershell
Set-Location apps\desktop
$env:VITE_STUDIO_API_URL = 'http://127.0.0.1:8765'
$env:VITE_STUDIO_TOKEN = 'studio-development-token-local-only'
npm run dev
```

Open `http://127.0.0.1:5173/`. The fixed token is for loopback development; the native host generates a token per launch. Health requires an Authorization bearer header. Ollama listens at `127.0.0.1:11434`.

On 2026-09-04, frontend HTTP returned 200 and authenticated backend health returned ready. Qwen's pinned digest matched, Power BI Desktop was discovered at version `2.157.1354.0`, and tools reported `pbi-cli 3.11.1`, authoring `0.1.4`, bridge `0.1.2`. This was a launch/readiness check, not a new model-generation or Desktop-render acceptance run.

## Behavior and limitations to remember

- Initial semantic definitions, measures and page recipes are deterministic; one optional bounded Qwen narrative call provides commentary. Revisions and advanced DAX have separate model generation paths. Do not describe every initial artifact as AI generated.
- Advanced DAX requires separate review and does not execute in the local logical preview. KPI cards without local results say they are calculated in Power BI.
- Vega previews are logical previews, not pixel-identical Power BI visuals. Some native visual families share a generic preview.
- Relationship candidates are validated using data evidence. Invalid candidates can be rejected; they must not be silently forced into the model.
- Existing PBIP handling is a compatibility reference plus managed-copy build, not arbitrary in-place migration. Unsupported model features remain blocked.
- Studio constructs `ModelSettings()` directly. Do not assume every value in the legacy YAML config is wired into the Studio runtime.
- Build definition validation and Desktop refresh/render verification are separate stages. Earlier render evidence exists; rerun verification for changed project output.
- The Windows installers built on 2026-09-01 are unsigned; Application Control blocked the extracted GUI in the previous session. Browser development remains the established launch path here.
- GUI and engine must have distinct names: `ai-powerbi-studio.exe` and `ai-powerbi-studio-engine.exe`. The packaging script, Tauri externalBin, capability and Rust launcher must agree.

## Verification and packaging entry points

- Dependencies and tools: `pyproject.toml`, `uv.lock`, `apps/desktop/package.json`, package lock, Tauri Cargo files.
- Backend tests: `tests/unit/test_studio_*.py`, `test_user_powerbi_builder.py`, governed measure and calendar integration tests.
- Routine checks: `uv run pytest`, `uv run ruff check src tests`, `uv run mypy src`, and `npm run build` in `apps/desktop`.
- Frontend unit tests: `npm test`. Isolated Edge end-to-end tests: `npm run test:e2e`.
- Release engineering runner: `scripts/verify-release.ps1 -Browser -Native`; real Power BI cycles: `scripts/qualify-studio-desktop.py`. See `docs/release-checklist.md` for scope and remaining release gates.
- Sidecar packaging: `scripts/build-desktop-sidecar.ps1`; Tauri packaging: `npm run tauri build`.
- Rust on this machine may require `C:\Users\developer\.cargo\bin` on the process PATH.
- Installer outputs: `apps/desktop/src-tauri/target/release/bundle/nsis/` and `bundle/msi/`.
- Legacy architecture and evaluation notes live in `docs/`; source and this index should guide current Studio work when those older notes differ.

## Repository condition

On the review date, `git status --short` showed the project content as untracked. Preserve this state; do not assume a commit baseline or discard files while making later changes. No source code, user draft, source snapshot or Power BI report was changed during this index-and-launch review.
