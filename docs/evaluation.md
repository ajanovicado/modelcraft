# Evaluation Protocol

## Corpus

`evals/cases/model_contract_cases.jsonl` contains 64 frozen requests:

| Family | Cases |
|---|---:|
| Build / Modify | 12 |
| Numerical analysis | 24 |
| Explain | 8 |
| Forecast / Scenario | 8 |
| Ambiguity | 6 |
| Adversarial / Unsupported | 6 |

Run the deterministic route baseline with:

```bash
uv run --extra dev ai-powerbi-analyst evaluate-routes
```

The output is saved under `evidence/model-runs`. It is explicitly route-only;
it does not count as semantic-plan, arithmetic, latency, memory, or model proof.

### Numerical golden contract

`evidence/golden-answers.jsonl` contains exactly the 24 numerical-analysis and
eight forecast/scenario cases, in frozen corpus order. Each line stores the
complete expected `AnalysisPlan`, `EvidenceAnswer`, rows and values, chart,
display facts, query-plan hash, data cutoff, metric references, `data_hash`,
and `catalog_hash`. The plan and answer are independently content-addressed;
the repeated fixture identity includes the manifest and all eight governed
source-file hashes.

Regenerate and validate the file only from the checked frozen fixture:

```bash
uv run python -m ai_powerbi_analyst.evaluation_goldens generate \
  --repository-root . --golden-path evidence/golden-answers.jsonl
uv run python -m ai_powerbi_analyst.evaluation_goldens validate \
  --repository-root . --golden-path evidence/golden-answers.jsonl \
  --output evidence/model-runs/numerical-golden-current.json
```

Validation executes all 32 governed plans against the local DuckDB, compares
the complete plan and answer exactly, and emits one measured outcome per case.
`exact_answer_rate` and `critical_fabrications` are derived from those outcomes;
an evaluator-supplied summary cannot override them. A content-addressed golden
file is expected test input, not proof that a candidate Ollama model selected
the correct plan or that Power BI Desktop reconciled the figures.

## Model run

Run every case three times per verified candidate using the same prompt,
catalog, temperature, context, output cap, and Ollama version. Record:

- candidate tag and digest;
- run and case IDs;
- expected/actual route;
- schema validity before and after the single permitted retry;
- selected metric, dimension, operation, page, and recipe IDs;
- numerical golden-answer result where applicable;
- rejection/clarification behavior;
- first-token and total latency;
- tokens per second;
- model/process/available memory;
- any attempted unsafe write.

On the pinned Windows host, use the repository-owned recorder rather than an
arbitrary evaluator executable or hand-authored summary:

```powershell
.\scripts\capture-phase0-windows.ps1 `
  -CaptureModelEvaluation `
  -ModelTag "qwen3.5:4b-q4_K_M" `
  -ModelDigest "sha256:<64 lowercase hex characters>"
```

For each numerical case, every repetition stores the actual plan and answer,
their content addresses, the frozen expected addresses, row/provenance/full
exactness flags, and explicit mismatch paths. The attestation validator reloads
`evidence/golden-answers.jsonl`, rebuilds each comparison from those raw
payloads, and derives the numerical score; receipt booleans cannot override it.

The release receipt is a closed, cross-checked contract rather than a summary
spreadsheet. It must contain one `candidate` identity (full tag and lowercase
`sha256:` digest), one content-addressed corpus with exactly 64 unique case IDs
and the family counts above, exactly three repetitions in frozen case order,
and an aggregate. Every repetition and the aggregate must include these four
families:

- `score`: route macro-F1, schema-valid-after-retry, accepted semantic-ID rate,
  unsafe writes, and critical hallucinations;
- `latency`: warm tokens/second, warm p95 seconds, model memory, and available
  memory;
- `repeatability`: route agreement and plan-schema agreement;
- `numerical`: exactly 32 numerical/forecast/scenario cases, exact-answer rate,
  and critical fabrications.

Receipt acceptance rejects booleans masquerading as numbers, NaN, infinity,
negative measurements, missing/duplicate case IDs, fewer or more than three
runs, candidate/digest drift, or a threshold failure in any individual run.

## Scores and hard stops

- Route macro-F1 ≥0.95.
- Schema-valid after one retry ≥98%.
- Accepted semantic IDs valid 100%.
- Numerical goldens match the exact governed rows, values, display text, chart,
  and provenance; display rounding is checked through the frozen rendered facts.
- Unsafe writes: zero.
- Critical fabricated numbers: zero.
- Warm ≥8 tokens/second preferred; below 6 triggers fallback.
- Model memory ≤3.3 GB on the 8 GB target.
- Available memory ≥750 MB without sustained paging.

Any unsafe write, critical fabricated number, catalog escape, or arithmetic
error is a hard failure regardless of aggregate score.

## Application and report evaluation

Model scores are only one layer. Separately run:

- unit and integration tests;
- exact DuckDB/Power BI metric reconciliation;
- query compiler adversarial tests;
- transaction fault injection;
- changed-file allowlist checks;
- repeated idempotency and refresh checks;
- strict-offline tests;
- pbi-cli and Microsoft validation;
- Desktop open/render inspection for every golden report;
- ten warm rehearsals and two cold-boot rehearsals.

## Evidence policy

Every **release** evidence package must include timestamp, git revision,
configuration hash, fixture hash, catalog hash, host facts, and tool/model
versions. Static corpora and expected-answer files are inputs rather than run
receipts. Local validator success is never recorded as Desktop render success.

See `evidence/README.md` for the authority and historical status of the bundled
artifacts. Historical screenshots, test counts, and browser receipts cannot be
promoted to current release evidence without a fresh run on the frozen
revision.
