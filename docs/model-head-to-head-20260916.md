> Historical local record. Referenced machine artifacts are excluded from this public source snapshot. Use the [README](../README.md) and [development guide](development.md) for current setup and tested scope.

# Windows model comparison — 16 September 2026

**Keep Qwen3.5:4b-q4_K_M as Studio's default.** The installed Qwen deployment substantially outperformed the installed MiniCPM GGUF under the same bounded conditions. Neither achieved perfect correctness. The new default uses an owned, guarded Ollama CPU process; the MiniCPM llama.cpp port and every earlier receipt remain available.

## Measured results

| Phase and policy | Qwen correct | MiniCPM correct |
|---|---:|---:|
| Initial 16 fresh tasks, original retrieval | **10/16** | **2/16** |
| Separate 8-task holdout, original retrieval | **5/8** | **1/8** |
| Same holdout, experimental retrieval + artifact checks | **3/8** | **1/8** |

Initial domain results: Qwen DAX 3/4, SQL 3/4, M 3/4, modeling 1/4; MiniCPM 0/4, 0/4, 0/4, 2/4 respectively.

The experimental policy retrieved one example from the requested language and added syntax/arity/schema checks. It regressed Qwen's M filtering and model-direction answers. **It was rejected.** Its corpus, code, outputs and failures remain frozen as evidence, but Studio retains the original two-example lexical retrieval.

A third, predeclared set of eight fresh schema variants compared Qwen's original retrieval with and without the added checks. Both scored **5/8**, with identical outputs and three incorrect artifacts accepted by both policies. This establishes no semantic accuracy gain from the checks. They are retained as targeted defenses against known malformed DAX/M, copied SQL schemas and invalid JSON; they do not replace execution or semantic review.

## Why this is a bounded deployment comparison

Both models used the same Ollama 0.33.3 engine, exact installed GGUF weights, identical task text/retrieved examples/JSON schema within each policy, greedy sampling, seed 9162026, four CPU threads, no GPU, no thinking, 4096 context tokens, 512 output tokens, 60 seconds per attempt, and at most one repair. First/second model and policy order alternated. Every request owned its daemon and workers, then unloaded them. No weights or adapters were trained.

The original direct llama.cpp pilot could not load Qwen's rope metadata. That pilot and a worker-shutdown/RAM-preflight pilot were preserved and excluded from quality scores. The common Ollama engine supports both installed packages. MiniCPM's template prompt-token counts agreed with the pinned formatter; full Ollama prompt token-ID parity was not established.

These are new names/data instances of known task families, not an unseen-family generalization test or an isolated performance benchmark. The Mac 14/14 result used a different harness/budget/runtime. Exact equivalence of the publisher's MiniCPM GGUF to the old pinned BF16 revision remains unverified. The findings justify the Windows default, not a universal ranking of the model families.

Initial observed median end-to-end times were about 29 seconds for Qwen and 16 seconds for MiniCPM; peak sampled model-tree working sets were about 3.77 and 1.84 GiB. These desktop observations are secondary to correctness, not speed guarantees.

## Scoring and review limits

SQL was executed against the supplied fresh rows in isolated DuckDB with external access disabled, one thread, 256 MiB and a three-second interrupt, checking output values, column names and order. M used Microsoft's parser plus manual semantic review. DAX and modeling used static diagnostics plus manual review by Codex, not an independent human. **No native Power BI execution of the benchmark DAX/M artifacts is claimed.**

Schema-valid JSON was not counted as a correct artifact. Missing answers, timeouts, wrong names, copied examples and semantically wrong queries failed. Fenced SQL/M failed the explicit directly-usable, no-fences contract. C12 MiniCPM's code would work after fence removal, but that repair was not delivered. C02's original wording did not explicitly say DAX; both models also failed its ratio semantics. Excluding C02 gives Qwen 10/15 versus MiniCPM 2/15.

C16 MiniCPM was credited for a valid proposed CDates[Date] design even though the reference used Day: the prompt allowed a new column name. Retaining receipt grain was implicit in its answer. Requiring an explicit one-row-per-receipt statement would lower MiniCPM to 1/16. Qwen's answer lacked the fact-side relationship key and proper cardinality. None of these sensitivities changes the model choice.

Important remaining failures include anti-joins that still include matched rows, M programs that add a column to one binding but return another, and incorrect relationship direction expressed in prose. A parser cannot establish those semantics.

## Changes applied

- Restored the exact original Qwen model package as the sole default candidate, through the new owned Ollama adapter. No silent model fallback.
- Kept the original corpus and retrieval. Added strict response validation and at most one bounded artifact repair, honoring callers that request zero repairs.
- Preserved 8 GiB free RAM/disk at startup, 3 GiB while running, AC power, 512 MiB maximum paging increase and the one-hour session cap. The owned process tree additionally has a kernel job-memory limit of 6 GiB plus a sampled aggregate RSS/private-memory guard.
- Kept conservative context admission and refused over-budget prompts/repairs rather than silently truncating. This can reject long app prompts before the nominal 4096-token limit; it is a documented limitation.
- Added Microsoft M syntax parsing with checks for selected function arities and literal table-row shape; added selected DAX arity/syntax checks and read-only SQL parsing/schema-name checks. The M helper runs without a console window.
- Fixed a validator false rejection found by the fresh app smoke: SUM(Minutes) in prose was mistaken for a table declaration. The production checker now distinguishes explicit typed/named tables from aggregate calls, compares names case-insensitively, and abstains when a schema cannot reliably be inferred. The frozen benchmark checker was not edited.
- Fixed deterministic relationship governance to select Both for verified one-to-one relationships, retain that rule on revalidation, and export explicit one-to-one cardinality. The exporter rejects an invalid single-direction one-to-one plan. Fresh Devices/Warranties tables verified the behavior and staging rejection.
- Rebuilt the Python engine, included the M parser bundle and license notices, and installed the matching engine beside the existing Studio executable. UI source was unchanged.

Power BI's one-to-one rule is documented by [Microsoft](https://learn.microsoft.com/en-us/power-bi/guidance/relationships-one-to-one). Relationship cardinality and direction properties are documented in the [Tabular Object Model](https://learn.microsoft.com/en-us/dotnet/api/microsoft.analysisservices.tabular.singlecolumnrelationship?view=analysisservices-dotnet). The M syntax parser is the [Microsoft Power Query parser](https://github.com/microsoft/powerquery-parser). M's lazy unused-binding behavior, relevant to C10, is specified in [the M let-expression specification](https://learn.microsoft.com/en-us/powerquery-m/m-spec-let).

## Verification and current readiness

Full unit suite: **701 passed, 3 skipped, 5 subtests passed**. Skips require unavailable Windows symlink privileges. After the SQL-checker fix, **14 targeted tests passed**. The new relationship export tests passed. Build completed successfully.

The first fresh app smoke delivered correct DAX, M and relationship artifacts. Its SQL model answer was correct but falsely rejected by the old checker; that failure was preserved. After fixing the checker, **two new SQL schemas both produced and executed correct results (2/2)**. Chat returned the expected text in four streamed chunks. These small checks do not imply general production accuracy.

The packaged health check verified Qwen's name, digest and Ollama backend. It refused inference at **7.9 GiB free RAM**, as required by the unchanged 8 GiB guard. Live inference was tested through the Python app adapter; packaged inference was not executed. The machine may need additional free memory before Studio can load the model. No unrelated system/security process was killed.

All model workers and smoke-engine processes were stopped at completion. All 51 final runtime pins matched. The 947 preserved historical research files and 34 frozen Windows replay inputs matched their earlier hashes. Prior default settings, source files and engine binaries were backed up before replacement. The earlier version-one runtime config and the adapter that exposed the SQL false rejection remain preserved.

## Evidence and use

- [Initial paired run and individual reviews](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/h2h-baseline-6880367ca85a4cc7b244658d680fe264/analysis.json)
- [Four-arm holdout results](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/h2h-holdout-2f0fad5df5514f01a6d3694393e77400/analysis.json)
- [Validation-only confirmation](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/h2h-confirmation-78dc1fc219c741dcba834aebe1102330/analysis.json)
- [Frozen tasks and protocol](C:/Users/developer/Desktop/AI-PowerBI-Analyst/evals/h2h_20260916_v3/protocol.json)
- [Initial app smoke and false-rejection review](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/qualified-app-eval-1c29c1dc0594464e8d9358da2bd684ba/review.json)
- [Fresh SQL confirmation](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/qualified-sql-eval-0918cbf7779240808d97fe756b68b378/results.json)
- [Packaged health receipt](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/qualified-package-smoke-38b0351df87f474f9af7e8c253650aab/health.json)
- [Final sealed receipt and hashes](C:/Users/developer/Desktop/AI-PowerBI-Analyst/.runtime/h2h-final-receipt-20260916.json)
- [Current model settings](C:/Users/developer/Desktop/AI-PowerBI-Analyst/config/defaults.yaml)
- [Pinned guarded runtime configuration](C:/Users/developer/Desktop/PowerBI-RAG/runtime/qualified-qwen-20260916-v2.json)
- [Studio executable](C:/Users/developer/Desktop/AI-PowerBI-Analyst/apps/desktop/src-tauri/target/release/ai-powerbi-studio.exe)

The next useful quality work is data-bound semantic checking and native Power BI evaluation of M/DAX, especially anti-joins, returned M bindings and filter propagation. Increasing retry/time budgets or training was not used to improve these scores.
