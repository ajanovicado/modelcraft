# GitHub Reference Brief: Local AI Dashboard Builder for Power BI

Checked: 2026-08-30  
Target: one CPU-only Windows laptop, local Ollama model, PBIP/PBIR report authoring, no Power BI Desktop required during normal app runtime.

## Executive conclusion

There is no single repository I would adopt as the application. There is, however, a strong reference stack:

1. Use [`MinaSaad1/pbi-cli`](https://github.com/MinaSaad1/pbi-cli) as the leading candidate for report-layer mutations.
2. Run Microsoft's [`powerbi-report-author`](https://github.com/microsoft/skills-for-fabric/tree/main/skills/powerbi-report-authoring) CLI as an independent PBIR metadata and validation oracle.
3. Read the sibling TMDL files directly for offline EXPLAIN mode; use [`pbip-tools`](https://github.com/iamahlramz/pbip-tools), [`pbip-documenter`](https://github.com/JonathanJihwanKim/pbip-documenter), and [`pbip-lineage-explorer`](https://github.com/JonathanJihwanKim/pbip-lineage-explorer) as parser and lineage references.
4. Use [`ollama-python`](https://github.com/ollama/ollama-python) for the first tool-calling smoke tests and orchestration prototype.
5. Build the application as an explicit Python state machine. Do not import a 50–139-tool MCP server or a general-purpose autonomous-agent loop into the MVP.

The most important finding is that offline formula explanation is possible for a “thick” PBIP project whose `definition.pbir` uses a local `byPath` reference and whose sibling `.SemanticModel/definition/` directory contains TMDL. In that configuration, the app can retrieve real measure DAX from disk without Power BI Desktop. For a thin `byConnection` report, the DAX is not present locally; EXPLAIN must either use a prepared manifest or be unavailable offline.

## What was verified directly

These checks were run against a real PBIR fixture from [`solid-data-public/powerbi-poc`](https://github.com/solid-data-public/powerbi-poc):

- `pbi-cli-tool==3.11.1` loaded on macOS without Power BI Desktop.
- `pbi --json report info`, `report validate`, `report list-pages`, and `visual list` returned parseable JSON without `pbi connect`.
- The report-layer commands successfully discovered one page and one `cardVisual` in the fixture.
- `@microsoft/powerbi-report-authoring-cli==0.1.4` validated the same fixture both with and without remote schema validation.
- Microsoft's CLI returned structured JSON for `preview-pages` and `preview-visuals`.

This strongly supports the headless report-layer premise. It does not replace the required verification on the target Windows laptop, especially for write operations and subsequent opening in Power BI Desktop.

## Ranked repositories

### Tier A — use or prototype against

| Repository | Best role | Recommendation | Important caveat |
|---|---|---|---|
| [`microsoft/skills-for-fabric`](https://github.com/microsoft/skills-for-fabric) | Authoritative PBIR authoring guidance; source for `powerbi-report-author` usage | **Adopt as the primary format reference and install its MIT-licensed validation CLI** | The CLI validates and inspects; it does not perform the report mutations needed by the app. Validator false positives have been reported, so retain a warning/override policy. |
| [`MinaSaad1/pbi-cli`](https://github.com/MinaSaad1/pbi-cli) | Headless report mutation commands for pages, visuals, bindings, filters, themes, and bookmarks | **Prototype as the write adapter** | Pin `pbi-cli-tool==3.11.1`. The package bundles Analysis Services DLLs and requires the prerelease `pythonnet==3.1.0rc0`, even when only the report layer is used. Treat installation and JSON contracts as things to verify, not assumptions. |
| [`microsoft/json-schemas`](https://github.com/microsoft/json-schemas) | Official PBIP/PBIR schemas and schema changelogs | **Vendor or cache only the schema versions referenced by the target fixture** | JSON Schema validates individual file shapes; it cannot prove that a visual's table/column/measure binding exists in TMDL. |
| [`ollama/ollama-python`](https://github.com/ollama/ollama-python) | Local streaming, tool calls, structured output, async client | **Use for the first orchestration and model smoke tests** | Ollama does not reliably enforce `tool_choice`; the application must detect missing/invalid calls and retry or fail closed. |
| [`iamahlramz/pbip-tools`](https://github.com/iamahlramz/pbip-tools) | Full TMDL parsing, visual-binding extraction, redaction, dependency audits | **Use as the strongest TMDL/parser design reference** | TypeScript and 75 MCP tools make the full server a poor fit for a tiny local model. Reuse a small package only if the Node sidecar is acceptable; otherwise implement the narrow manifest extractor in Python using its tests and edge cases as references. |
| [`JonathanJihwanKim/pbip-documenter`](https://github.com/JonathanJihwanKim/pbip-documenter) | Offline model/report documentation and “where used” analysis | **Borrow MIT-licensed ideas for EXPLAIN context construction** | Browser-oriented JavaScript, not a ready Python dependency. |
| [`JonathanJihwanKim/pbip-lineage-explorer`](https://github.com/JonathanJihwanKim/pbip-lineage-explorer) | PBIR → measure → DAX → source-column lineage and impact analysis | **Reference its core parser and lineage tests** | Richer than Phase 1 requires; start with exact measure retrieval and one-level dependency extraction. |
| [`promptfoo/promptfoo`](https://github.com/promptfoo/promptfoo) | Comparing local models on routing, tool selection, argument validity, and grounded output | **Use selectively for model-level evaluation** | End-to-end report mutation and rollback are easier to score in the Python test suite. Use Promptfoo for LLM behavior, pytest for application behavior. |

### Tier B — borrow patterns, not the whole product

| Repository | What is worth studying | Why not make it the foundation |
|---|---|---|
| [`jonathan-pap/powerbi-report-mcp`](https://github.com/jonathan-pap/powerbi-report-mcp) | Closest open-source clone: PBIR pages, visuals, bindings, formatting, Zod output schemas, composite/batch calls, layout validation, and a read-only eval fixture | It exposes 56 tools by default and remains MCP/TypeScript-oriented. Its own “minimal” set is still too broad for a 4B model. Use its tool schemas to design 3–4 intent-specific Python tools. |
| [`akhilannan/pbir-utils`](https://github.com/akhilannan/pbir-utils) | Python-native metadata extraction, wireframe visualization, rules, sanitization, filter/theme utilities, and a local web UI | It is oriented toward maintenance and bulk utilities, not complete page/visual construction. It could become an optional inspection/quality dependency. |
| [`NatVanG/fab-inspector`](https://github.com/NatVanG/fab-inspector) | Deterministic local PBIR/TMDL quality rules with JSON output; overlap, visual-quality, governance, and CI patterns | .NET and a declarative rules engine add weight. Best as an optional final quality gate, not the per-write validator. The predecessor [`PBI-Inspector`](https://github.com/NatVanG/PBI-Inspector) does not support modern PBIR. |
| [`HorizunGroup/horizun-pbi-mcp`](https://github.com/HorizunGroup/horizun-pbi-mcp) | Atomic JSON writes, backup/re-read guarantees, transaction rollback tests, confirmation gates, schema pinning, stdout/stderr discipline, and large safety test coverage | It exposes 139 tools and spans live TOM, DAX, TMDL, PBIR, conversion, and document export. That scope is the opposite of the constrained MVP, but its safety mechanics are excellent reference material. |
| [`rajivdatta/mcp-powerbi-plus`](https://github.com/rajivdatta/mcp-powerbi-plus) | A small Python/FastMCP implementation of 14 PBIR read/write tools, model-field reading, and in-memory validation-before-write | The author explicitly says its writes have not yet been validated against a report opened in Power BI Desktop. Treat it as readable sample code, not proven infrastructure. |
| [`C-QY/pbi-check-loop`](https://github.com/C-QY/pbi-check-loop) | Restart/reopen/screenshot loop for proving that PBIR actually renders in Desktop | Useful during development and before recording the demo, but it deliberately requires and restarts Desktop. It cannot be part of the normal low-RAM runtime. |
| [`Juls-BI/pbir-a11y`](https://github.com/Juls-BI/pbir-a11y) | Accessibility score and deterministic checks over PBIR folders | PolyForm Shield licensing restricts competing products. Run as an external development check only after reviewing the license; do not copy its implementation. |
| [`FabricTools/pbir-samples`](https://github.com/FabricTools/pbir-samples) | MIT-licensed PBIP/PBIR fixture corpus | Samples can drift from the exact Desktop/schema version you pin. Normalize fixtures and keep at least one project authored by the target Desktop version. |

### Tier C — useful to know about, but avoid in Phase 1

| Repository | Verdict | Reason |
|---|---|---|
| [`maxanatsko/pbir.tools`](https://github.com/maxanatsko/pbir.tools) | **Reference behavior only** | Technically impressive and agent-oriented, but its custom license is non-commercial and expressly forbids derivative works without consent. Do not copy or build the portfolio project around it if future commercial use matters. |
| [`Babymcnerd/pbir-tool`](https://github.com/Babymcnerd/pbir-tool) | **Study the declarative YAML compiler idea only** | It has a good spec → validate → backup → write → preview pipeline and extensive tests, but the repository has no license file. Unlicensed code should not be copied or redistributed. |
| [`microsoft/powerbi-modeling-mcp`](https://github.com/microsoft/powerbi-modeling-mcp) | **Defer** | It is a public-preview semantic-model server with specific EULA restrictions and known safety/behavior issues. It also expands the exact semantic-authoring scope deliberately excluded from Phase 1. |
| [`pbi-tools/pbi-tools`](https://github.com/pbi-tools/pbi-tools) | **Reference only** | Mature Power BI DevOps/extraction tooling, but AGPL-3.0, PBIX-oriented, and not a modern PBIR visual-authoring API. |
| [`TabularEditor/TabularEditor`](https://github.com/TabularEditor/TabularEditor) | **Reference only** | Excellent semantic-model tooling and MIT-licensed, but C#/.NET and centered on the model layer rather than PBIR report authoring. |
| [`microsoft/tmdl-parser`](https://github.com/microsoft/tmdl-parser) | **Do not depend on it** | Despite its description, the public repository currently contains only a template README and no parser source. |
| [`Atzingen/tmdl-parser`](https://github.com/Atzingen/tmdl-parser) | **Do not adopt as the sole parser** | Lightweight and MIT, but its parser is narrow, assumes a simple project naming convention, and does not cover the complete TMDL tree robustly. |
| [`kohsah/tmdl2json`](https://github.com/kohsah/tmdl2json) | **Fixture/reference only** | More tests and more coverage than the smaller Python parser, but very small adoption and a hand-written parser that needs broad real-project validation. |

## Recommended architecture using the references

```text
User request
    │
    ▼
Deterministic intent router
    ├── EXPLAIN ──► local semantic manifest + PBIR inventory ──► short grounded prose
    ├── BUILD   ──► narrow page/visual spec ──► write adapter
    └── MODIFY  ──► current report state + target selector ──► write adapter
                                                  │
                                                  ▼
                                        pbi-cli mutation
                                                  │
                                                  ▼
                            pbi-cli structural validation
                                      + Microsoft schema/catalog validation
                                      + custom PBIR↔TMDL binding audit
                                                  │
                                failure ──────────┴────────── success
                                  │                              │
                            capped repair                  commit result
                                  │                              │
                            restore snapshot                update UI state
```

### Component mapping

- **Authoritative format knowledge:** `microsoft/skills-for-fabric` + `microsoft/json-schemas`.
- **Writes:** thin subprocess wrapper around the released `pbi-cli-tool` report commands.
- **Independent validation:** `@microsoft/powerbi-report-authoring-cli`.
- **Cross-layer binding audit:** a narrow local TMDL manifest, informed by `pbip-tools` and `pbip-lineage-explorer`.
- **EXPLAIN retrieval:** exact DAX, descriptions, format strings, fields, relationships, page/visual summaries; never free-form model memory.
- **Model client:** `ollama-python` initially; model name and endpoint remain configuration.
- **Orchestration:** ordinary Python functions plus Pydantic models, not LangGraph or a free-form agent loop.
- **UI:** Streamlit chat/status primitives; keep orchestration independent of Streamlit session state.
- **Evaluation:** pytest for full workflows and file outcomes; Promptfoo only where it makes cross-model comparison easier.
- **Optional development gates:** Fab Inspector for report quality, PBIR A11y for accessibility if its license is acceptable, and `pbi-check-loop` for pre-demo render verification.

## Proposed minimal internal tool surface

The model should never see the upstream repositories' complete command catalogs. A good first BUILD tool surface is:

1. `inspect_context()` — returns the allowed pages, fields, measures, and existing visuals in a compact schema.
2. `create_or_select_page(page_spec)` — creates one page or resolves an existing target.
3. `upsert_visual(visual_spec)` — creates or modifies one visual from a constrained visual spec.
4. `validate_changes()` — runs all validators and returns normalized repair hints.

EXPLAIN should use no write tools:

1. `find_report_object(query)` — deterministic search over manifest, pages, and visuals.
2. `get_grounding(object_ids)` — returns exact DAX/TMDL/PBIR excerpts within a token budget.

Filters and formatting can initially be fields inside `visual_spec`, not separate model-visible tools. This keeps the local model's choice problem small.

## Validation strategy

No one validator is sufficient:

1. **Structure and local invariants:** run `pbi report validate` immediately after a mutation.
2. **Official PBIR schemas and visual metadata:** run `powerbi-report-author validate`; use `catalog describe` and `formatting describe-*` before constructing unfamiliar visual roles or properties.
3. **Semantic binding integrity:** independently cross-reference every PBIR field projection against the local TMDL manifest. Microsoft's JSON schemas cannot perform this cross-file semantic check.
4. **Layout quality:** deterministic overlap, off-canvas, minimum-size, and tab-order rules. This can be custom code initially or an optional Fab Inspector pass.
5. **Rendered truth:** before the recorded demo, open/reload in Desktop and inspect every changed page. This is a release/demo gate, not a runtime dependency.

## Immediate next verification pass

1. On the target Windows laptop, pin and install `pbi-cli-tool==3.11.1` and `@microsoft/powerbi-report-authoring-cli==0.1.4` in isolated environments.
2. Run the complete report-layer help inventory and save golden JSON outputs.
3. Copy a Desktop-authored PBIP fixture, close Desktop, and use `pbi-cli` to add one page and one bound `cardVisual`.
4. Run both validators, then open the project in Desktop and confirm rendering.
5. Parse the fixture's `.SemanticModel/definition/tables/*.tmdl` into a minimal manifest and answer one measure explanation without connecting to Desktop.
6. Only then run Ollama's official `tools.py`, `multi-tool.py`, and structured-output examples against the three candidate models.

If these six checks pass, the original project is not only viable; it has a cleaner foundation than the handoff assumed: community mutations, Microsoft validation/metadata, and direct offline TMDL retrieval.
