# Unified authoring workspace

Implemented September 6, 2026. This is a workflow simplification, not a new model,
Power BI compiler, or release attestation.

## User workflow

- **Workspace:** one active conversation per workspace, with Report, Data, and
  Model tabs. "New chat" archives the current conversation behind a boundary;
  cleared conversations stay readable under History and remain in the local
  evidence store, but no longer feed the model's context.
- **Report:** the previous preview and Designer are one source-backed editing view.
  Properties appear only after selecting a visual. Hide Conversation expands the
  working area; narrower windows use a collapsible conversation drawer.
- **Data:** import and inspect sources. “Ask about…” attaches the selected table to
  the shared conversation and prepares a question. Nothing is submitted automatically.
- **Model:** inspect relationships and formulas. Measure and relationship shortcuts
  use the same conversation, with an explicit removable context chip.
- **Review & build:** review the draft, select relationships to include, explicitly
  review advanced DAX, approve, then build. Open and Save As are available after a
  committed build. Errors and progress remain visible inside the review dialog.
- **Activity & versions:** build history, export, project rollback, and expandable
  evidence/validation details. Settings stays separate.

## Behavior and safeguards

The selected context contains strict entity IDs; the engine resolves names against
the active workspace and rejects unknown or mismatched references. Context does
not convert a question into a write request. Page-relative requests such as “add a
chart to this page” resolve against the attached page. Ambiguous targets remain
subject to the existing planner checks.

Chat creates/revises only the logical draft, never approves it or writes Power BI
files. A successful revision shows the actual before/after change summary and
selects the changed report page. Explanation-only responses are labeled as such.

Report edits stay mounted across authoring tabs, Activity, and Settings. Unsaved
edits block AI changes, imports, builds, and workspace replacement; refresh/close
raises the browser's unsaved-changes warning. They are not persisted after an
explicit reload or process exit. Save validates; Reset discards only unsaved edits.

Draft Undo restores a server-owned SQLite revision of the same draft and imported
snapshot, checks the current base hash, revalidates, recomputes previews, and revokes
overall draft approval and advanced-DAX approval. Previously reviewed relationships
for the unchanged snapshot remain reviewed. No PBIP bytes change. Draft Undo is
separate from project rollback in Activity; the quick Undo target is session-local.

## Verification

Run from the repository root:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests/unit -p test_unified_workspace.py -v
uv run ruff check src/ai_powerbi_analyst/studio tests/unit/test_unified_workspace.py
uv run mypy src/ai_powerbi_analyst/studio
cd apps\desktop
npm.cmd test
npm.cmd run build
npm.cmd run test:workspace
```

`test:workspace` uses the running development app and an existing populated
workspace (`Superstore Local` by default, configurable via `STUDIO_TEST_WORKSPACE`).
It reads existing state, blocks unexpected mutations, and mocks the two chat/undo
responses. It checks one composer, navigation, preserved edits, context transmission,
revision feedback, undo, approval reset, unique DOM IDs, dialog keyboard focus and
the narrow drawer. It does not call an LLM or write an actual Power BI project.

The isolated Python suite exercises real ingestion/planning/storage and authenticated
API routes with temporary test sources. It checks new-draft creation through chat,
page-scoped additions, cancellation, non-mutating discussions, strict context,
server-side restoration, stale/foreign revision rejection and advanced-DAX reset.

The existing real-model/Power-BI release scenario in `e2e/studio.spec.ts` has been
updated for the shared conversation and central review. Running that scenario,
Desktop rendering and native installer acceptance remain separate release gates.
The Vite build still reports the existing large lazy-loaded Vega bundle warning.

## Running-engine update

Restart the Python engine after installing these changes. Vite updates the UI only.
`/v1/ping` advertises `chat-context`, `draft-restore`, and `chat-clear`; an old engine triggers a
visible restart notice. Contextual submission is rejected locally with the input
preserved, and draft Undo is disabled until supported. No security policy changes
or alternate process launch are needed.
