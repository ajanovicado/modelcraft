# Implementation Plan: Local Report Studio

Proposed plan — 2026-08-30

## Outcome

Deliver a local Windows application that can:

1. inspect a prepared PBIP project without Power BI Desktop running;
2. explain report/model objects using exact local PBIR and TMDL evidence;
3. translate a supported plain-English Build or Modify request into a typed plan;
4. preview that plan before any write;
5. compile it into deterministic report-layer operations on a staged copy;
6. validate, promote, diff, and undo the result safely; and
7. demonstrate the entire workflow offline on a CPU-only 8–16 GB laptop.

The MVP is complete only when the four-part demo—Explain, Build, Modify, Recovery—passes ten consecutive warm rehearsals and the accepted PBIP subsequently opens in the pinned Power BI Desktop version.

## The key architecture decision

Use the local model as a **translator and explainer, not an operator**.

The model may:

- classify a request into Explain, Build, Modify, Clarify, or Unsupported;
- select a tested recipe and exact IDs from a bounded project manifest;
- produce a short explanation from retrieved evidence.

The model may not:

- emit CLI commands, shell fragments, paths, raw PBIR JSON, raw coordinates, DAX, or arbitrary formatting properties;
- choose a free-form sequence of tools;
- decide whether a failed candidate is safe to commit;
- repair files directly.

The normal Build path is:

    user request
        ↓
    capability preflight and deterministic retrieval
        ↓
    one schema-constrained planning call
        ↓
    typed plan validation and user approval
        ↓
    deterministic recipe compiler
        ↓
    staged pbi-cli mutations
        ↓
    layered validation and changed-file policy
        ↓
    promote + re-read + diff + undo

### Why a typed plan beats a tool loop

| Approach | Small-model reliability | Inference cost | Testability | MVP decision |
|---|---:|---:|---:|---|
| Free agent over a large CLI/MCP catalog | Low | Highest | Low | Reject |
| State machine where the model chooses each next tool | Medium | High | Medium | Benchmark only |
| State machine + typed plan + recipe compiler | High | One planning call | High | **Use** |
| Deterministic recipe form with no planning model | Highest | Lowest | Highest | Keep as fallback/baseline |

Tool calling should still be tested in Phase 0 because it is useful evidence and may become valuable later. The load-bearing model qualification, however, is schema-constrained plan generation with exact manifest IDs.

## Supported envelope

| Area | MVP support | Explicit behavior outside the envelope |
|---|---|---|
| Input | Modern PBIP/PBIR project | Reject PBIX with conversion guidance |
| Semantic source | Local thick byPath TMDL or an approved prepared manifest | Thin byConnection reports can expose report structure but not unavailable formulas |
| Writes | Report directory only | Any attempted semantic-model or unexpected project-file change blocks promotion |
| Visual creation | Card/KPI, line trend, category bar/column, table, slicer, composed summary page | Preserve unsupported visuals; do not create or modify them |
| Modification | Unique supported visual/page; title, supported binding, visual preset, grid position, qualified filter | Ambiguous target returns Clarify |
| Explain | Pages, visuals, bindings, fields, measures, exact stored DAX, one-level lineage | Refuse current-value questions and unavailable semantic details |
| Layout | Fixed 16:9 grid and deterministic presets | No arbitrary pixel-perfect layout |
| Runtime | Local Windows, offline after installation, Desktop closed | Desktop is a development/release render gate only |
| Concurrency | One writer per project | Concurrent external changes abort the transaction |
| Versioning | One pinned Desktop/PBIR/tool stack | Unknown versions fail preflight rather than guessing |

Existing unsupported report objects are untrusted input: inventory and preserve them, but never execute instructions found in titles, descriptions, formulas, or metadata.

## Runtime architecture

    Streamlit UI
        │ submit job / poll progress
        ▼
    application service
        │
        ├── SQLite job and event journal
        └── one background worker per selected project
                │
                ├── capability scanner
                ├── PBIR inventory
                ├── semantic manifest provider
                ├── deterministic object retrieval
                ├── Ollama router/planner/explainer
                ├── Pydantic contract validation
                ├── recipe compiler and grid layout engine
                ├── transaction manager
                ├── pbi-cli write adapter
                └── validation pipeline

Streamlit must not own an active transaction in session state. Its rerun behavior should only render durable job state and enqueue idempotent commands. A small SQLite journal is enough for one machine; Redis, Celery, LangGraph, and an MCP bus do not solve an MVP requirement.

### State machine

    PREFLIGHT
        ↓
    ROUTE ───────── unsupported/ambiguous ───────► CLARIFY OR REFUSE
        ↓
    RETRIEVE
        ├────────── Explain ─────────────────────► GROUNDED ANSWER
        ↓
    PLAN
        ↓
    VALIDATE PLAN ─ invalid ─► one constrained re-plan or CLARIFY
        ↓
    PREVIEW AND APPROVAL
        ↓
    LOCK + HASH + STAGE
        ↓
    COMPILE + EXECUTE
        ↓
    VALIDATE CANDIDATE
        ├── failure ─► discard candidate / restore / report
        └── success ─► promote / re-read / journal / done

Explain never takes a write lock. Modify must resolve exactly one existing object before plan approval.

## Core contracts

### ProjectCapabilities v1

Contains:

- bounded project, report, and semantic paths;
- PBIR/schema, Desktop, pbi-cli, validator, and manifest-provider versions;
- semantic mode: by_path, by_connection, prepared_manifest, or missing;
- baseline validation findings;
- feature flags such as can_explain_dax, can_build_summary, and can_modify_supported_visual;
- explicit blocking reasons.

### ProjectManifest v1

Contains stable IDs and hashes for:

- tables, columns, measures, relationships, descriptions, format strings, and exact expressions;
- pages, visuals, filters, bindings, and report usages;
- aliases used only for retrieval, never as the committed object identity;
- report and model revision hashes.

The manifest is the only vocabulary from which an accepted model plan may choose fields or objects.

### RequestEnvelope v1

A closed union:

- ExplainQuery
- BuildRequest
- ModifyRequest
- Clarification
- UnsupportedRequest

Unknown keys and values are rejected. No path, command, JSON fragment, or executable field exists in this schema.

### ReportChangePlan v1

Includes:

- plan and schema version;
- base report and manifest hashes;
- user-visible assumptions;
- recipe enum;
- exact manifest IDs;
- logical visual keys;
- supported filter/style options;
- maximum operation count;
- a closed union of EnsurePage, AddRecipeVisual, ModifyRecipeVisual, RemoveOwnedVisual, SetSupportedFilter, and ApplyStylePreset.

It does not include generated PBIR IDs, raw positions, CLI arguments, DAX, or arbitrary properties. The compiler owns those.

### ValidationReport v1

Every normalized finding has:

- stable code;
- source validator;
- blocking, warning, or advisory severity;
- affected object ID and JSON pointer where available;
- new, changed-object, or baseline-existing status;
- repairability and user-facing explanation.

The report must expose render_verified as false during headless runtime. It changes only after an explicit Desktop release check.

### TransactionRecord v1

Records:

- transaction and request IDs;
- base, candidate, promoted, and undo hashes;
- staged and backup locations;
- command receipts;
- allowlisted and actual changed files;
- validation reports;
- promotion/recovery stage;
- repair, rollback, and final status.

## Semantic manifest decision

Do not begin by writing a general TMDL parser.

Define a replaceable SemanticManifestProvider and qualify these options in order:

1. A small .NET helper using Microsoft TOM’s documented TmdlSerializer.DeserializeModelFromFolder or DeserializeDatabaseFromFolder.
2. A narrowly packaged extractor informed by the MIT-licensed pbip-tools parser and test cases.
3. A prepared companion manifest for the curated fixture and for thin reports.

Microsoft documents direct folder deserialization into the TOM object model, making an offline helper credible. The current API documentation also flags prerelease behavior, so package compatibility, redistribution, cold-start cost, memory, and real-model coverage are Phase-0 gates—not assumptions. See the [TmdlSerializer API](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.tmdlserializer?view=analysisservices-dotnet) and [TMDL overview](https://learn.microsoft.com/en-us/analysis-services/tmdl/tmdl-overview?view=sql-analysis-services-2025).

The provider interface keeps this choice out of orchestration, Explain, and validation code.

## Transaction and validation design

### Transaction protocol

1. Acquire a per-project lock.
2. Resolve and re-check that every path stays beneath the selected PBIP root.
3. Hash the complete base project.
4. Copy the complete PBIP root to a transaction staging directory so relative byPath references remain valid.
5. Mutate only the candidate report directory.
6. Compare changed paths against an allowlist; block any semantic-model or unexpected project change.
7. Run the full validation stack.
8. Re-hash the original to detect concurrent edits.
9. Promote with a journaled directory swap: original report to retained backup, candidate report to the original location.
10. Re-read and validate the promoted report.
11. Retain a bounded, hash-addressed undo history.

Fault-inject process termination at every stage. Recovery must leave either the complete old report or the complete new report, never a mixture.

### Validation layers

1. Parse every changed JSON file.
2. Validate against locally vendored Microsoft schemas matching the pinned project.
3. Run pbi-cli structural validation and normalize its JSON response.
4. Run Microsoft’s report-authoring CLI as an independent format/metadata oracle.
5. Cross-reference every app-created PBIR binding against the semantic manifest.
6. Assert recipe postconditions: expected page and logical visual exist once, field roles/cardinality match, layout is bounded, and IDs are unique.
7. Reject unexpected changed files.
8. Run deterministic layout checks: off-canvas, overlap, minimum size, duplicate names, and supported tab order.
9. Compare all findings with the preflight baseline.

Commit is blocked by:

- any new validator error;
- any error attached to a changed object;
- any unresolved app-created binding;
- any failed recipe postcondition;
- any unexpected file change;
- an unknown blocking diagnostic;
- a changed base hash.

Pre-existing unrelated warnings may remain visible without blocking only after an explicit diagnostic-code policy is written and tested. Do not blanket-ignore official warnings.

A model may receive normalized errors and produce one replacement plan. It never edits the failed candidate. The replacement compiles again from the clean staged baseline. Known layout and role repairs belong in deterministic code.

## Proposed repository layout

    pyproject.toml
    README.md
    scripts/
      verify_environment.ps1
      inventory_tool_contracts.ps1
      run_offline_demo.ps1
    src/pbi_local/
      config.py
      domain/
        capabilities.py
        manifest.py
        requests.py
        report_plan.py
        results.py
      project/
        loader.py
        inventory.py
        selectors.py
      manifest/
        provider.py
        tom_sidecar.py
        prepared.py
      adapters/
        pbi_cli.py
        report_author.py
        ollama_client.py
      recipes/
        base.py
        kpi.py
        trend.py
        category.py
        table.py
        slicer.py
        executive_overview.py
        modify.py
      execution/
        compiler.py
        transaction.py
        recovery.py
      validation/
        pipeline.py
        schemas.py
        bindings.py
        layout.py
      explain/
        index.py
        retrieval.py
        service.py
      orchestration/
        router.py
        planner.py
        service.py
      jobs/
        store.py
        worker.py
      ui/
        app.py
        components.py
        results.py
      eval/
        runner.py
        scorers.py
    tests/
      unit/
      integration/
      e2e/
      contracts/
      fixtures/
    evals/
      cases.jsonl
    evidence/
      environment.json
      cli-contracts/
      model-runs/
      render-checks/

## Dependency graph

    Phase 0 Windows viability evidence
       ├── pinned CLI contracts
       ├── target fixture and golden outputs
       ├── semantic manifest provider decision
       └── model contract benchmark
                    │
                    ▼
    Phase 1 domain contracts + loader + transaction + adapters + validation
                    │
                    ▼
            hardcoded deterministic recipe
                    │
             ┌──────┴────────┐
             ▼               ▼
    Phase 2 Explain     Phase 3 Build/Modify
             └──────┬────────┘
                    ▼
              Phase 4 UI/jobs
                    ▼
          Phase 5 evaluation/hardening
                    ▼
             Phase 6 demo release

The critical path is Windows mutation/reopen → safe transaction/validation → hardcoded recipe → typed Build/Modify → repeated demo.

## Work breakdown

Estimates assume one developer with agent assistance, roughly six productive hours per focused day, and regular access to the target Windows laptop.

### Phase 0 — viability and evidence

Time box: 2.5–4 focused days. Do not write application code before Gate A.

#### 0.1 Capture the target baseline

Files:

- scripts/verify_environment.ps1
- evidence/environment.json
- evidence/README.md

Acceptance:

- Record Windows build, CPU, physical RAM, Python, Node, Ollama, Power BI Desktop, pbi-cli, and Microsoft validator versions.
- Record exact model tags/digests on the test day.
- Confirm Desktop is closed during every headless run.
- Prove the required dependencies can run with non-loopback network blocked after installation.

Verification:

- Run the PowerShell evidence script from a fresh terminal.
- Compare its JSON output with a checked-in schema.
- Repeat from a cold boot.

Depends on: none.

#### 0.2 Freeze report-layer command and JSON contracts

Files:

- scripts/inventory_tool_contracts.ps1
- evidence/cli-contracts/commands.json
- tests/contracts/test_cli_contracts.py

Acceptance:

- Inventory the installed help for every intended report, visual, filter, and formatting command.
- Capture success and failure JSON for each wrapped command.
- Pin pbi-cli-tool 3.11.1 and Microsoft report-authoring CLI 0.1.4 for the initial spike, then update only through an explicit version decision.
- Ten repeated read/write runs on disposable fixture copies produce parseable receipts with Desktop closed.

Verification:

    pbi --version
    pbi --help
    pbi report --help
    pbi visual --help
    pbi filters --help
    pbi format --help
    pytest tests/contracts/test_cli_contracts.py -q

Exact mutation syntax must come from this installed inventory; do not copy it from planning notes.

Depends on: 0.1.

#### 0.3 Prove Windows mutation and Desktop reopen

Files:

- scripts/run_mutation_matrix.ps1
- evidence/render-checks/mutation-matrix.json
- tests/fixtures/sales/

Acceptance:

- With Desktop closed and network blocked, create and modify the candidate core visuals on staged fixture copies.
- Run both external validators and the first binding audit.
- Reopen every accepted candidate in the pinned Desktop version.
- Apply every recipe twice and prove idempotence: no duplicates and the same normalized final state.

Pass threshold:

- 10/10 repeated mutation sequences open and render.
- 10/10 pass required validators.
- 10/10 idempotence checks.

Depends on: 0.2.

#### 0.4 Select the semantic manifest provider

Files:

- spikes/manifest_probe/
- evidence/manifest-provider-comparison.md
- evidence/manifest-example.json

Acceptance:

- Detect byPath, byConnection, prepared-manifest, and missing semantic modes.
- Test the TOM folder deserializer first, then a narrow parser/provider only if needed.
- Extract exact tables, columns, measures, DAX, descriptions, format strings, relationships, hidden status, and direct report usage.
- Test at least 20 representative TMDL projects/fixtures, including quoted names, multiline expressions, hidden fields, cultures, relationships, calculation groups, and field parameters.
- Unsupported constructs downgrade capabilities explicitly; none disappear silently.

Pass threshold:

- Exact extraction for every object claimed as supported.
- Formula retrieval works offline with Desktop closed.
- Thin reports return offline_formula_unavailable rather than a fabricated answer.
- Cold-start and steady memory fit the target laptop.

Depends on: 0.1.

#### 0.5 Benchmark the local-model contract

Files:

- spikes/model_contract_benchmark.py
- evals/model-contract-cases.jsonl
- evidence/model-runs/model-smoke.csv

Compare:

- one trivial Ollama tool call;
- a short structured RouteDecision;
- a schema-constrained ReportChangePlan using exact manifest IDs.

Run 30 cases per verified candidate model before the larger evaluation corpus.

Pass threshold:

- first-pass schema-valid output at least 27/30;
- valid after one retry at least 29/30;
- no invented enum or ID in any accepted output;
- at least 8 generated tokens/second preferred;
- 6–8 tokens/second is a warning requiring tighter UX/output;
- below 6 tokens/second causes a model/scope pivot;
- model resident memory at most 3.3 GB on the 8 GB target;
- the model, app, and browser preserve at least 750 MB available memory without sustained paging;
- p95 planning completion at most 45 seconds.

Depends on: 0.1 and a small manifest from 0.4.

### Gate A — go, pivot, or stop

Proceed to the deterministic kernel only if:

- Windows writes are Desktop-openable and repeatable;
- both validators return machine-readable results;
- one semantic manifest path works offline;
- one local model meets the structured-plan floor;
- the strict offline run has no hidden download;
- all versions and golden contracts are frozen.

Pivot rules:

- If pbi-cli writes fail, allow a two-day direct-PBIR recipe compiler spike.
- If that spike cannot create a safe Desktop-openable card and chart, stop Build and ship Explain + QA.
- If tool calling fails but structured output passes, proceed exactly as planned.
- If every model fails the structured-plan or speed floor, use a deterministic recipe form for Build and reserve the model for grounded Explain.
- Do not let Phase 0 drift beyond four focused days without a written decision.

### Phase 1 — deterministic safety kernel

Estimate: 4–6 focused days.

#### 1.1 Define closed domain contracts

Files:

- src/pbi_local/domain/manifest.py
- src/pbi_local/domain/requests.py
- src/pbi_local/domain/report_plan.py
- tests/unit/test_domain_models.py

Acceptance:

- Closed enums and discriminated unions reject unknown fields and values.
- All object references are canonical manifest IDs.
- No model-controlled path, command, raw JSON, coordinate, or DAX field exists.
- Serialization and plan hashing are deterministic.

Verification:

    pytest tests/unit/test_domain_models.py -q

Depends on: Gate A.

#### 1.2 Load, bound, inventory, and fingerprint a project

Files:

- src/pbi_local/project/loader.py
- src/pbi_local/project/inventory.py
- src/pbi_local/manifest/provider.py
- tests/unit/test_project_loader.py
- tests/integration/test_manifest_provider.py

Acceptance:

- Resolve one report and its semantic source beneath the selected root.
- Reject escaping paths, PBIX, unknown schema, and unsafe cloud-synced/concurrently edited locations in the initial release.
- Produce stable IDs and revision hashes.
- Build an explicit capability result for thick, thin, prepared-manifest, and unsupported projects.

Depends on: 1.1 and 0.4.

#### 1.3 Normalize external tool adapters

Files:

- src/pbi_local/adapters/pbi_cli.py
- src/pbi_local/adapters/report_author.py
- src/pbi_local/adapters/ollama_client.py
- tests/integration/test_adapters.py

Acceptance:

- Spawn argument arrays without shell interpolation.
- Separate stdout JSON, stderr, exit code, duration, and tool version.
- Turn timeouts, malformed JSON, nonzero exits, and version mismatch into typed errors.
- Run pbi-cli in an isolated process so prerelease pythonnet behavior cannot crash Streamlit.
- Contract tests match the frozen installed-version receipts.

Depends on: 0.2 and 1.1.

#### 1.4 Implement the staged transaction and recovery journal

Files:

- src/pbi_local/execution/transaction.py
- src/pbi_local/execution/recovery.py
- tests/unit/test_transaction.py
- tests/integration/test_recovery.py

Acceptance:

- Mutations occur only on a full staged PBIP copy.
- Only allowlisted report files can change.
- Base-hash drift aborts promotion.
- Undo restores the exact prior hash.
- Termination at every state transition recovers to a valid complete old or new report.

Pass threshold:

- 25/25 injected failures recover exactly.
- Zero semantic-model writes.

Depends on: 1.2.

#### 1.5 Build the layered validator

Files:

- src/pbi_local/validation/pipeline.py
- src/pbi_local/validation/schemas.py
- src/pbi_local/validation/bindings.py
- src/pbi_local/validation/layout.py
- tests/integration/test_validation_pipeline.py

Acceptance:

- Run all validation layers and normalize findings.
- Separate blocking, warning, advisory, and baseline-existing findings.
- Catch planted nonexistent fields/measures, wrong roles, invalid JSON, off-canvas layout, overlap, duplicate logical objects, and unexpected file changes.
- Unknown new diagnostics block until reviewed.

Pass threshold:

- 10/10 planted binding/role faults detected.
- No blocking false positive on the canonical fixtures.

Depends on: 1.2 and 1.3.

#### 1.6 Compile one hardcoded executive-overview recipe

Files:

- src/pbi_local/recipes/executive_overview.py
- src/pbi_local/execution/compiler.py
- tests/e2e/test_hardcoded_overview.py

Acceptance:

- Build one page containing a KPI, time trend, and category comparison from fixed manifest IDs.
- Assert exact roles, layout, unique logical keys, and expected changed-file scope.
- Reapplying the same plan is idempotent.
- A deliberately invalid binding is rejected and the original restored.

Pass threshold:

- 10/10 fresh builds pass the complete validation transaction.
- 10/10 open and render in Desktop during the release check.

Verification:

    pytest tests/unit -q
    pytest tests/integration -m windows -q
    pytest tests/e2e/test_hardcoded_overview.py -q

Depends on: 1.4 and 1.5.

### Gate B — safe deterministic authoring

Required:

- 100% rollback correctness;
- 10/10 validated, idempotent hardcoded builds;
- 10/10 Desktop render checks;
- zero writes outside the report directory;
- every planted app-created binding fault detected.

If the gate fails, reduce the visual/formatting surface before adding the model.

### Phase 2 — Explain vertical slice

Estimate: 2.5–4 focused days. This can proceed in parallel with late deterministic recipe work after the manifest contract is stable.

#### 2.1 Build deterministic retrieval

Files:

- src/pbi_local/explain/index.py
- src/pbi_local/explain/retrieval.py
- src/pbi_local/project/selectors.py
- tests/unit/test_retrieval.py

Use exact names, aliases, lexical scoring, and a small local full-text index. Do not add an embedding model to the MVP memory budget.

Acceptance:

- Exact-name queries resolve top-1 in 100% of supported cases.
- Paraphrases place the correct object in top-3 at least 95% of the time.
- Duplicate names produce Clarify.
- Context includes only matched objects, direct bindings, and one dependency level within a fixed token budget.

Depends on: 1.2.

#### 2.2 Generate and verify grounded answers

Files:

- src/pbi_local/explain/service.py
- src/pbi_local/domain/results.py
- tests/e2e/test_explain.py

The structured answer contains:

- short summary;
- exact source IDs;
- facts tied to those IDs;
- limitations.

The UI, not the model, renders the exact stored DAX expression.

Pass threshold:

- grounded factual precision at least 95%;
- zero fabricated object names or formulas;
- every factual answer has a valid source;
- thin-report formula refusal is correct 100%;
- warm p95 completion at most 60 seconds.

Depends on: 2.1 and 1.3.

#### 2.3 Freeze the Explain corpus

Files:

- evals/explain-cases.jsonl
- src/pbi_local/eval/scorers.py
- evidence/explain-baseline.json

Acceptance:

- At least 25 blind questions spanning measures, formulas, fields, visuals, pages, usages, ambiguity, missing evidence, and prompt-injection-like metadata.
- Automatic source-ID checks plus human correctness labels.
- Any critical unsupported claim blocks Gate C.

Depends on: 2.2.

### Gate C — trustworthy Explain

If free-form prose fabricates report facts, replace it with deterministic explanation templates plus an optional clearly labeled summary. Explain may be narrow; it may not invent.

### Phase 3 — typed Build and Modify slices

Estimate: 5.5–8 focused days.

#### 3.1 Route, retrieve, and clarify

Files:

- src/pbi_local/orchestration/router.py
- src/pbi_local/orchestration/service.py
- tests/unit/test_router.py

Acceptance:

- Deterministic high-confidence rules handle obvious requests; a short structured classifier handles the remainder.
- Route macro-F1 is at least 0.95.
- Every Explain, Unsupported, and Clarify result is non-writing.
- Semantic authoring, DAX, relationships, RLS, PBIX, and arbitrary visual requests are refused clearly.

Depends on: 1.1, 2.1, and 0.5.

#### 3.2 Produce a typed change plan

Files:

- src/pbi_local/orchestration/planner.py
- src/pbi_local/prompts/build_plan.txt
- tests/e2e/test_plan_generation.py

Acceptance:

- The model chooses only recipe, canonical manifest IDs, qualified filter options, and presentation intent.
- First-pass schema validity is at least 90%; validity after one re-plan is at least 98%.
- Every accepted field/object ID resolves to the current manifest.
- Base-hash mismatch, ambiguity, or second failure stops safely.

Depends on: 3.1 and Gate B.

#### 3.3 Add the supported recipe library

Files:

- src/pbi_local/recipes/base.py
- src/pbi_local/recipes/kpi.py
- src/pbi_local/recipes/trend.py
- src/pbi_local/recipes/category.py
- tests/e2e/test_build_recipes.py

Acceptance:

- Each recipe declares exact role/cardinality rules, layout behavior, compiler operations, postconditions, fixtures, and eval cases.
- Recipes compose on a fixed grid without collision or off-canvas output.
- Every accepted plan passes the full transaction and validator pipeline.

The table and slicer recipes are added only after KPI, trend, and category meet the reliability threshold.

Depends on: 1.6 and 3.2.

#### 3.4 Add bounded modification

Files:

- src/pbi_local/recipes/modify.py
- src/pbi_local/project/selectors.py
- tests/e2e/test_modify.py

Acceptance:

- Rename page/visual title, replace a supported binding, switch a supported visual preset, move/resize on the fixed grid, or change one qualified filter.
- Modify an existing unique object instead of silently creating a duplicate.
- Target accuracy is at least 95% and zero critical wrong-object modifications.
- Ambiguity produces Clarify.

Depends on: 3.2 and 3.3.

#### 3.5 Implement bounded repair and safe failure

Files:

- src/pbi_local/validation/repair_hints.py
- src/pbi_local/orchestration/planner.py
- src/pbi_local/execution/recovery.py
- tests/e2e/test_recovery.py

Acceptance:

- Deterministic fixes handle known packing, naming, and role-normalization issues.
- A model may re-plan once from normalized findings.
- Re-plan starts from the clean staged baseline.
- Unrecoverable cases restore exactly.
- Result status distinguishes applied, repaired, restored, rejected, and ambiguous.

Pass threshold:

- known recoverable cases repair at least 90% on the first bounded attempt;
- unrecoverable faults restore 100%;
- no workflow exceeds one model re-plan.

Depends on: 3.3, 3.4, and 1.4.

### Gate D — authoring alpha

Run at least 24 cases: ten Build, eight Modify, three ambiguous, and three unsafe/unsupported.

Required:

- supported end-to-end success at least 90% first attempt;
- at least 95% after one bounded repair;
- accepted candidates passing all validators 100%;
- wrong-object critical modifications zero;
- unsafe requests causing writes zero;
- warm p95 completion at most 120 seconds.

If reliability is 80–89%, retain only recipes individually meeting 90%. If it remains below 80% after one prompt/schema revision, stop expanding natural-language Build and ship Explain plus a recipe wizard.

### Phase 4 — durable jobs and Streamlit UX

Estimate: 2.5–4 focused days.

#### 4.1 Add durable jobs and progress

Files:

- src/pbi_local/jobs/store.py
- src/pbi_local/jobs/worker.py
- src/pbi_local/ui/app.py
- tests/ui/test_workflow_state.py

Acceptance:

- UI submits idempotent jobs and polls an event journal.
- Time to first visible progress is at most one second.
- Long work emits a progress event at least every five seconds.
- Refresh/reconnect never duplicates an applied transaction.
- One writer is enforced per project.

Depends on: Gate C and Gate D.

#### 4.2 Add preflight, plan approval, evidence, and Undo

Files:

- src/pbi_local/ui/components.py
- src/pbi_local/ui/results.py
- tests/ui/test_results.py

Acceptance:

- Preflight visibly distinguishes thick, thin, prepared-manifest, and unsupported projects.
- No write occurs before Apply.
- Results show affected objects, before/after hashes, validator status, repair/restore status, and the distinction between local validation and Desktop render proof.
- One-click Undo restores the prior hash.
- Errors remain useful without exposing stack traces, hidden prompts, or raw untrusted metadata.

Depends on: 4.1.

### Gate E — demo UX

Ten complete scripted runs must show:

- no duplicate mutation;
- no unexplained pause longer than five seconds;
- correct status and validation evidence;
- successful Undo;
- no Desktop process during app runtime.

### Phase 5 — model selection and hardening

Estimate: 3–5 focused days.

#### 5.1 Run the full model scorecard

Files:

- evals/cases.jsonl
- src/pbi_local/eval/runner.py
- src/pbi_local/eval/scorers.py
- evidence/model-runs/

Corpus: 32 cases.

- 6 Build
- 6 Modify
- 6 measure/formula Explain
- 4 page/visual Explain
- 4 ambiguous/Clarify
- 6 adversarial/unsupported

Run every candidate three times at temperature zero with the exact shipping prompt, context, quantization, and Ollama template: 96 runs per model.

Capture raw output, schema result, selected IDs, validator result, final hash, retries, latency, tokens/second, peak memory, and paging.

Hard floors:

- router macro-F1 at least 0.95;
- accepted schema-valid plans after one retry at least 98%;
- supported end-to-end success at least 90%;
- accepted-output validation 100%;
- critical hallucinations zero;
- unsafe writes zero;
- warm generation at least 6 tokens/second, 8 preferred;
- model resident memory at most 3.3 GB;
- every individual demo request succeeds at least four out of five times.

Models failing a hard floor are disqualified before weighting.

Weighted score:

| Dimension | Weight |
|---|---:|
| End-to-end supported-task success | 30% |
| Plan and route correctness | 20% |
| Safety | 15% |
| Repeatability | 10% |
| Latency | 10% |
| Memory | 10% |
| Explanation clarity | 5% |

Depends on: Gate E.

#### 5.2 Run strict offline and fault suites

Files:

- scripts/run_offline_demo.ps1
- tests/e2e/test_fault_matrix.py
- evidence/offline-run.json

Acceptance:

- Block non-loopback networking and complete the entire workflow.
- Fault-inject every transaction state and every external-tool error class.
- Confirm no runtime schema/package/model download.
- Confirm model prompt injection in report metadata cannot alter control flow.

Depends on: 5.1.

#### 5.3 Run Desktop render truth checks

Files:

- evidence/render-checks/release-matrix.json
- evidence/render-checks/screenshots/
- docs/render-verification.md

Acceptance:

- Open every golden accepted candidate in the pinned Desktop version.
- Inspect each changed page and record Desktop version, fixture hash, screenshot, and pass/fail.
- Keep this outside the production runtime.

Depends on: 5.2.

### Phase 6 — demo and release

Estimate: 1.5–3 focused days.

#### 6.1 Freeze the four-part demo

1. Explain: “How is Total Revenue calculated, and where is it used?”
2. Build: “Create an Executive Overview with Total Revenue and Gross Margin cards, monthly revenue trend, and revenue by region.”
3. Modify: “Rename the page Leadership Snapshot, change Revenue by Region to a horizontal bar chart, and show the top five regions.”
4. Recovery: enable a clearly labeled validation-test mode that injects a bad field binding after execution, then show detection and repair or exact rollback.

The injected case is deliberate and repeatable. Do not gamble on an uncontrolled model failure during the recording.

Release threshold:

- ten consecutive warm runs;
- two complete cold-boot runs;
- no unreviewed warning;
- no internet after setup;
- Desktop closed during runtime;
- final accepted project opens and renders in Desktop afterward.

#### 6.2 Document the evidence

Files:

- README.md
- docs/architecture.md
- docs/evaluation.md
- docs/limitations.md
- THIRD_PARTY_NOTICES.md

Acceptance:

- State supported recipes, thick-PBIP requirement, non-goals, exact pins, model digest, fixture hash, evaluation method, known limitations, and validator/render distinction.
- Cite reference implementations without copying unlicensed or restricted code.
- Include the measured reason for choosing the final model and rejecting alternatives.

Depends on: 6.1.

## Delivery estimate

| Phase | Focused days | Primary deliverable |
|---|---:|---|
| 0. Viability | 2.5–4 | Evidence-backed go/pivot/stop decision |
| 1. Deterministic kernel | 4–6 | Safe hardcoded headless build |
| 2. Explain | 2.5–4 | Grounded read-only assistant |
| 3. Build/Modify | 5.5–8 | Typed recipes and safe modifications |
| 4. UI/jobs | 2.5–4 | Demoable, restart-safe Streamlit flow |
| 5. Evaluation/hardening | 3–5 | Model choice and reliability evidence |
| 6. Demo/docs | 1.5–3 | Frozen portfolio release |
| Contingency | 4–5 | Windows, PBIR, and model surprises |

Expected total: **about 25–35 focused days**, roughly five to seven full-time weeks or eight to twelve part-time weeks. The plan intentionally spends the first few days buying down the risks that could otherwise invalidate weeks of UI work.

## Repository adoption decisions

### Use directly behind adapters

- [MinaSaad1/pbi-cli](https://github.com/MinaSaad1/pbi-cli): pinned report-layer mutation candidate.
- [microsoft/skills-for-fabric](https://github.com/microsoft/skills-for-fabric): authoritative report-authoring guidance and independent validation CLI.
- [microsoft/json-schemas](https://github.com/microsoft/json-schemas): vendor only the pinned schemas required for offline validation.
- [ollama/ollama-python](https://github.com/ollama/ollama-python): structured output, streaming, and model client.

### Borrow patterns and tests, not product scope

- [iamahlramz/pbip-tools](https://github.com/iamahlramz/pbip-tools): TMDL/binding parsing, redaction, lineage, and edge cases.
- [jonathan-pap/powerbi-report-mcp](https://github.com/jonathan-pap/powerbi-report-mcp): typed visual schemas, composite operations, layout checks, and fixtures—not its 56-tool MCP surface.
- [HorizunGroup/horizun-pbi-mcp](https://github.com/HorizunGroup/horizun-pbi-mcp): transaction, confirmation, atomic-write, schema-pin, and recovery patterns—not its 139-tool scope.
- [JonathanJihwanKim/pbip-documenter](https://github.com/JonathanJihwanKim/pbip-documenter) and [pbip-lineage-explorer](https://github.com/JonathanJihwanKim/pbip-lineage-explorer): Explain, where-used, and one-level lineage behavior.
- [FabricTools/pbir-samples](https://github.com/FabricTools/pbir-samples): fixture corpus, supplemented by a project authored in the exact target Desktop version.

### Evaluation/development-only helpers

- [promptfoo/promptfoo](https://github.com/promptfoo/promptfoo): optional cross-model plan/tool behavior comparisons; pytest remains the authority for file outcomes.
- [NatVanG/fab-inspector](https://github.com/NatVanG/fab-inspector): optional final quality gate.
- [C-QY/pbi-check-loop](https://github.com/C-QY/pbi-check-loop): Desktop render/reload ideas for development and pre-demo verification only.

### Avoid as foundations

- Broad MCP servers that expose dozens or hundreds of tools to the small model.
- pbir.tools as copied code because of its restrictive license.
- Unlicensed declarative compiler implementations.
- Semantic-model authoring MCPs during report-layer Phase 1.
- A handwritten general TMDL parser before the official TOM/provider spike.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| pbi-cli command or JSON drift | Pin version, isolate adapter, freeze golden contracts, fail preflight on mismatch |
| Prerelease pythonnet instability | Dedicated process/environment, timeouts, crash containment |
| PBIR schema drift | Pin Desktop/schema/tool generation; vendor only required schemas; reject unknowns |
| Official validator false positives | Baseline by diagnostic code, review deliberate exceptions, never blanket-ignore |
| Schema-valid but semantically broken bindings | Mandatory PBIR-to-manifest audit and recipe postconditions |
| Model invents a field | Accepted plans can contain only current canonical manifest IDs |
| Wrong object gets modified | Stable IDs, unique selector, user preview, Clarify on ambiguity |
| Model emits commands or paths | Domain schema contains no such fields |
| CPU wait feels frozen | One short model call, compact context, output cap, cached manifest, immediate event timeline |
| Thin report lacks local DAX | Preflight downgrade; prepared manifest or honest refusal |
| Valid report renders poorly | Deterministic layouts plus Desktop release gate; never mislabel local validation |
| Recovery corrupts files | Full staging, journaled promotion, hashes, fault-injection suite |
| Cloud sync/concurrent edits | Initial local-directory constraint, project lock, base-hash recheck |
| Malicious report metadata affects the model | Treat PBIR/TMDL as untrusted data, delimit evidence, structured output, no model-controlled execution |
| License contamination | Third-party notice, dependency audit, copy only compatible licensed code |

## Stop conditions

Pivot the general builder to Explain + QA if any remains true after the bounded spikes:

- no headless path can create a Desktop-openable report;
- rollback cannot be proven exact;
- writes cannot be confined to the report directory;
- every local candidate falls below 90% typed-plan reliability or below 6 tokens/second;
- supported Build success remains below 80% after one schema/prompt revision;
- the ten-run rehearsal remains below 90% after cutting scope;
- any unresolved path allows the model to mutate the semantic model.

## Scope-cut order

If time slips, cut in this order:

1. themes;
2. bookmarks;
3. advanced filters;
4. table and slicer recipes;
5. multi-page generation;
6. nonessential formatting.

Do not cut:

- grounded Explain;
- KPI, trend, and category recipes;
- one reliable Modify flow;
- plan approval;
- staged transactions and Undo;
- independent validation and binding audit;
- Windows Desktop render verification;
- the evidence-backed evaluation report.

## Definition of done

- A fresh pinned setup launches on the target laptop.
- Power BI Desktop remains closed throughout normal app operation.
- The four-part demo succeeds ten consecutive times.
- Every accepted mutation passes both external validators, the semantic-binding audit, layout checks, and changed-file policy.
- Every failed mutation restores the exact prior report.
- Explain produces no critical fabrication on the golden corpus.
- The chosen and rejected models are documented with CPU, RAM, latency, repeatability, and task-success measurements.
- The final PBIP opens and renders correctly in the pinned Desktop version.
- The README clearly states support boundaries and known limitations.

## Immediate next action

Approve and execute **Phase 0 only**. Its output should be an evidence package and a written Gate A decision. Do not scaffold Streamlit or the orchestration package until Windows write/reopen, offline manifest retrieval, model structured output, memory, and strict-offline behavior are all measured.
