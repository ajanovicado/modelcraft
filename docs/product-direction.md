# Product Direction: Local Report Studio

Decision proposal — 2026-08-30

## Executive decision

Build **Local Report Studio: a safe, local compiler for Power BI report intent**.

It should not behave like a general autonomous agent. The local model translates plain language into a small, typed `ReportChangePlan`; deterministic code resolves exact model objects, chooses commands, lays out visuals, validates a staged copy, and either commits or restores it.

The user-facing promise is:

> **Private, reviewable, reversible Power BI report changes that explain their own evidence.**

This combines four of the strongest ideas into one coherent product:

- **Safe Report Operator** is the product: request, preview, approve, validate, diff, undo.
- **Recipe Composer** is how Build stays dependable: tested report recipes rather than arbitrary PBIR generation.
- **Explain and Lineage** grounds questions and shows why a metric or visual exists.
- **QA and Recovery** is the trust layer around every write.

The chat box is only an input method. The differentiated product is the compiler and safety envelope behind it.

## The sharper problem

**How might we help a Power BI report owner in a privacy-sensitive environment turn a business request into a safe, reviewable PBIP change—while proving what changed and why—without cloud AI or Power BI Desktop running?**

The initial user should be narrower than “any nontechnical business user.” A credible first user is a **Power BI analyst or report owner working against an approved, prebuilt semantic model**. A business stakeholder can phrase the request, but an analyst prepares/selects the PBIP project and remains the final approver.

Their job to be done is:

> When I receive a report request against an approved model, I want to create or modify the report quickly and understand exactly what the system did, so I can deliver confidently without hand-authoring every visual or sending report metadata to the cloud.

## Directions considered

Scores are 1–5. The weighted total uses user value 35%, feasibility 30%, differentiation 20%, and portfolio/demo proof 15%.

| Product direction | Value | Feasibility | Differentiation | Demo | Weighted | Decision |
|---|---:|---:|---:|---:|---:|---|
| General autonomous Power BI copilot | 5 | 1 | 3 | 5 | 3.40 | Reject for MVP; too much stochastic control |
| **Safe local report operator** | 5 | 4 | 4 | 5 | **4.50** | **Lead product** |
| **Explain and lineage assistant** | 4 | 5 | 4 | 4 | **4.30** | **Build first; fallback product** |
| **Recipe-driven dashboard composer** | 4 | 5 | 3 | 5 | **4.25** | **Internal Build strategy** |
| **Report QA and repair assistant** | 4 | 4 | 5 | 4 | **4.20** | **Trust layer and differentiator** |
| Executive report-pack generator | 4 | 2 | 4 | 5 | 3.55 | Defer until one-page builds are boringly reliable |
| Local PBIP intelligence/indexing platform | 3 | 4 | 4 | 3 | 3.50 | Enabling infrastructure, not the product story |
| Desktop-driven visual feedback agent | 3 | 2 | 2 | 4 | 2.65 | Development/release gate only |

### What each serious direction teaches us

1. **Explain-first is the fastest honest wedge.** It is read-only, can cite exact PBIR/TMDL objects, and remains useful even if report mutation fails.
2. **Recipe-driven Build is the reliable form of generation.** The model selects a tested recipe and exact manifest IDs; code owns field roles, coordinates, formatting presets, and command order.
3. **Modification is more valuable than greenfield generation.** Existing reports are where real work accumulates. A unique-target requirement, plan preview, and diff make modification safer than an unrestricted editor.
4. **QA/recovery supplies a stronger story than “AI made a chart.”** Independent validation, cross-layer binding checks, staged writes, and undo demonstrate engineering judgment and user trust.
5. **A general tool-calling agent is the wrong control plane.** It requires repeated slow inference and gives a 4B model responsibility for sequencing, low-level arguments, and recovery—the exact work deterministic software does better.

## Recommended experience

1. The user selects a prepared PBIP project.
2. A compatibility screen states what is locally available: report structure, semantic model, formula explanation, supported write recipes, pinned versions, and baseline warnings.
3. The user asks a question or requests a change.
4. For Explain, the app resolves real objects and returns a short answer with source cards and the exact stored formula where relevant.
5. For Build or Modify, the app shows its interpretation: target page/visual, exact measures and fields, selected recipe, filters, and planned changes.
6. The user approves the plan.
7. The app changes a staged copy, runs layered validation, checks the allowed diff, and promotes only a valid candidate.
8. The result shows what changed, validation evidence, the limits of that evidence, and Undo.
9. On failure, the app makes at most one bounded re-plan from normalized errors; otherwise it restores the clean state and explains why it stopped.

The interface should distinguish **validated locally** from **render-verified in Power BI Desktop**. Headless validation is valuable, but it is not proof of pixel-perfect rendering.

## Portfolio MVP

Prove one coherent job:

> **Turn a request into a validated executive-summary page against a prepared semantic model, allow one precise modification, and explain the metrics used.**

Include:

- One curated “thick” PBIP fixture with a local `byPath` TMDL semantic model.
- Compatibility preflight with clear rejection and capability downgrades.
- Grounded Explain for a measure, field, page, and visual.
- One executive-summary recipe containing KPI cards, a time trend, and a category comparison.
- One uniquely targeted modification path: title, selected measure/category, or qualified Top-N filter.
- Typed plan preview and explicit approval.
- Staged transaction, independent validation, result diff, retained snapshot, and Undo.
- An injected-error scenario that visibly demonstrates safe rejection or rollback.
- A fully offline CPU-only run with measured latency, throughput, and memory.

The supported creation surface should initially be limited to card/KPI, line trend, category bar/column, table, slicer, and the composed summary-page recipe. Existing unsupported objects may be inspected and preserved, but not modified.

## Must-be-true assumptions

| Assumption | Killer test | Pass condition | If it fails |
|---|---|---|---|
| Headless mutations create Desktop-openable PBIR | Perform ten representative creates/modifications on the target Windows version and reopen every result | 10/10 open and render; repeated plans are idempotent | Pivot mutation backend or ship Explain + QA |
| A small local model can create the typed plan | Run at least 60 mixed requests against the real manifest on target hardware | ≥95% route accuracy; ≥98% schema-valid after one retry; zero invented IDs; ≥90% correct recipe/bindings | Use deterministic recipe forms for Build and keep natural language for Explain |
| Local PBIR + TMDL is enough to ground Explain | Blind-test at least 25 questions | Correct cited object/formula; explicit “not available locally” when absent; zero critical unsupported claims | Require a prepared manifest or narrow Explain scope |
| Transactions are genuinely recoverable | Terminate the worker at every snapshot/stage/promote transition | 100% recover to a valid old or new report; never a partial state | Stop all writes until fixed |
| Users accept bounded recipes for stronger trust | Test plan/approval prototype with five likely report owners | They can complete a frequent task and prefer/reliably understand the bounded workflow | Revisit the target job before expanding features |
| The target inventory is compatible | Interview likely users and inspect real samples | Enough PBIP, PBIR, and local semantic sources exist to make the wedge relevant | Reposition toward developer/analyst tooling or prepared-project workflows |

## Not doing in the MVP

- Semantic model creation, DAX authoring, relationships, Power Query, calculation groups, refresh, or RLS.
- PBIX binary editing.
- Arbitrary visual types, custom visual mutation, free-form PBIR JSON, or pixel-perfect layout generation.
- A broad MCP/tool catalog exposed directly to the model.
- Automatic writes without preflight, plan preview, approval, staging, validation, and Undo.
- Formula/source claims for thin `byConnection` reports without a prepared local manifest.
- Questions that require executing DAX or seeing current data values.
- Bookmarks, drillthrough, advanced interactions, field parameters, or complex conditional formatting until individually qualified.
- Multi-page executive packs before the single summary-page recipe is repeatable.
- Cloud inference, publishing, tenant administration, collaboration, authentication, or multi-user hosting.
- Power BI Desktop automation during normal runtime.
- An installer before Windows feasibility and offline packaging gates pass.
- Marketing the MVP as universal self-service for every Power BI user.

## Fallback that still ships

If the Windows mutation/reopen gate fails, do not ship a flaky builder. Ship **Local Report Explain + QA**:

- grounded measure/page/visual explanations;
- report inventory and lineage;
- binding, layout, and schema diagnostics;
- safe repair recommendations without writes;
- a compatibility report showing exactly why Build is unavailable.

That remains a coherent, useful, and technically credible portfolio product while the mutation backend is replaced or narrowed.

## Why this is the best portfolio story

The project demonstrates constrained local inference, a domain-specific intermediate representation, deterministic compilation, source-grounded explanation, version-aware validation, transactional file operations, fault recovery, and evaluation on real hardware. That is materially more impressive—and more defensible—than another chat wrapper around a large command catalog.
