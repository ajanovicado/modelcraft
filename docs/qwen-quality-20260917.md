# Qwen quality and memory improvements — 17 September 2026

The default remains **Qwen3.5:4b-q4_K_M, CPU, inference only**. The installed runtime now checks selected request constraints before returning artifacts, renders single relationships from structured fields, and caps the saved-prompt cache at **512 MiB**. The weights and original worked-example corpus are unchanged.

The final paired factory/chat check produced **3/4 correct artifacts versus 2/4 for the previous warm policy**. Both failed the M task. This is a small comparison using new schemas and data in familiar task families, not a general accuracy estimate or a reproduction of the Mac 14/14 result.

The current configuration is `PowerBI-RAG/runtime/qualified-qwen-20260917-v1.json`, with **58 verified hash pins**. Both installed desktop sidecars have been rebuilt. **Live packaged inference remains unverified:** the packaged app refused to start the model at approximately 7.65 GiB free RAM, below the unchanged 8 GiB requirement. Source inference did pass that requirement. Packaged startup and cleanup were exercised, with no remaining owned workers.

## What changed

- **DAX artifacts:** check explicitly requested measure names, declared tables/columns, delimiters, and unexpected trailing prose. Strings and comments stay opaque. These are selected structural checks, not a complete DAX parser or semantic evaluator. A fresh test caught an extraction bug that treated “using CALCULATE” as part of the name; it was fixed and separately replayed successfully.
- **SQL artifacts:** bind a read-only SELECT against empty tables built from explicitly supplied typed schemas. This catches undefined aliases, missing/ambiguous columns, and invalid aggregation. External access and extension loading are disabled, with one thread, a 256 MB connection limit, and a three-second interrupt. No user rows are executed by this validation step. Unrecognized schema formats abstain from binding; existing checks still apply.
- **Single relationship artifacts:** require both endpoint references, cardinality, active status, and filter direction as JSON fields. Check supplied constraints and render the description deterministically, avoiding omitted status and unrelated UI instructions. General explanations and multi-relationship plans retain their original response path. Power BI one-to-one relationships require Both filtering, including when configured inactive. [Microsoft guidance](https://learn.microsoft.com/en-us/power-bi/guidance/relationships-one-to-one).
- **Explicit, self-contained artifact chats:** validate a complete artifact before yielding it. Ordinary discussion keeps streaming. Artifact buffering delays visible output until validation finishes; cancellation before that output remains bounded by the existing generation timeout rather than being proven immediate.
- **Saved-prompt memory:** set the bundled llama-server's `LLAMA_ARG_CACHE_RAM` to 512 MiB. Warm model reuse, the live context, and the original guards remain. The installed binary's help and runtime logs confirm this setting. The upstream cache behavior and environment setting are also described in [Ollama's issue tracker](https://github.com/ollama/ollama/issues/18264).

All repairs remain limited to the existing maximum of one. No training, larger model, GPU change, corpus rewrite, or replacement of the frozen benchmark judge was used. The Studio's separate typed dashboard-planning and review workflow remains in place.

## Evidence and rejected experiments

Task payloads were saved before model output. Both sides used the same frozen model, deterministic decoding settings, original retrieval corpus, resource guards, and task data. The later paired runs used the same 512 MiB prompt-cache limit. Different candidate versions are reported separately; replays are not counted as fresh tasks.

| Stage | Control | Candidate | Interpretation |
|---|---:|---:|---|
| Initial 12 fresh tasks | 10/12 | v1: 11/12 | Relationship omissions/false claims fixed; candidate hit an allocation failure on the final M request |
| Eight untouched H tasks | 5/8 | v2: 6/8 | Relationship improvement; both retained a bad SQL alias and an M syntax failure |
| Twelve development replays after H | — | v2: 12/12 | Regression checks, not fresh evidence |
| Ten new N tasks | 8/10 | v3: 7/10 | Rejected: name-parser false rejection; harder SQL and M still failed |
| Two earlier failures replayed in v3 | — | 1/2 | SQL binding repaired H5; experimental M guidance produced semantically wrong H7 |
| Four fresh V factory/chat tasks | 2/4 | final v4: 3/4 | Selected policy; M still failed in both arms |
| N1 name-parser regression | — | final v4: 1/1 | Correct CALCULATE measure accepted after the parser fix |

The M experiment added a valid syntax example to repair feedback. It did not fix N9 and produced a parsable but incorrect sort key in H7: `{{[Key], Order.Ascending}}` used an unbound field access where the intended column-name text was required. **That repair change was removed.** The original M generation/repair policy and parser remain. M parsing does not establish M execution semantics; see the supported sorting forms in [Microsoft's Table.Sort documentation](https://learn.microsoft.com/en-us/powerquery-m/table-sort).

In the selected policy, the new SQL binder caught the prior undefined alias `m` while the query declared `mc` and `pc`. One bounded repair produced correct executable SQL. This was a diagnostic replay, not a fresh accuracy gain. A harder join in N6 still copied incorrect table names and was withheld by the existing validator.

SQL evaluation used the unchanged frozen judge and actual DuckDB execution on the supplied rows. M syntax was parsed with the existing Microsoft parser bundle and semantics reviewed manually. DAX and relationship semantics were checked statically and manually. **No native Power BI, DAX, or M semantic execution was performed.** Detailed judgments, including failures and the accepted ambiguity in control N4, are in `.runtime/qwen-quality-20260917/semantic-review-notes.json`.

## Latency and memory

The final paired requests used the same client prompts and system instructions. The candidate ran first through the real factory, followed by the original warm-runtime control. Initial loading is included in V1.

| Fresh request | Control | Candidate | Strict result |
|---|---:|---:|---|
| V1, DAX sum with “using SUM” | 22.094 s | 20.063 s | Both correct |
| V2, inactive one-to-one relationship | 20.187 s | 17.672 s | Candidate correct; control gave incorrect filtering advice |
| V3, SQL grouped charges through chat | 11.891 s | 13.015 s | Both correct; E=25, F=30 |
| V4, M calculated column through chat | 25.422 s | 45.703 s | Control returned invalid pseudo-M; candidate withheld invalid M after its repair |

These mixed outcomes do **not** establish a general speed improvement. Validation and a failed repair can increase response time. The previously measured warm-session optimization is retained; its controlled speed evidence remains in the [September 16 report](qwen-speed-20260916.md).

The uncapped v1 log showed an 8192 MiB saved-prompt cache allowance and ended in `std::bad_alloc`. Its recorded process-tree private-memory peak reached 5.77 GiB before the failure. The capped 20-request v2 run processed every request without an allocation failure, recorded a 4.86 GiB private-memory peak, and logged cache eviction within 512 MiB. One M validation failure ended its first session; the second session handled 13 requests. These are observations across different request sequences, not a normalized memory benchmark.

Startup still requires 8 GiB free RAM/disk and AC power. Running still requires 3 GiB free RAM, with the existing 512 MiB paging bound, 6 GiB process protections, one-hour session clock, exclusive lock, STOP_REQUESTED handling, 60-second generation attempts, four CPU threads, 4096-token context, and conservative no-truncation input bound. Warm sessions still unload after 120 idle seconds.

## Validation, installation, and preservation

- Full application suite: **787 passed, 3 skipped, 5 subtests passed**. The skips require unavailable Windows symlink privilege. Changed-file Ruff checks passed.
- PyInstaller rebuilt successfully. Both the bundled sidecar and portable release sidecar were updated. The GUI executable and installer were not rebuilt.
- Installed engine SHA-256: `ea631184719e9c1b2a964ee5c1df0bba65af516692c4760f5983c03635401fb4`.
- Two packaged attempts were blocked by the unchanged memory guard, at 7.64 and 7.65 GiB. Both cleaned up fully. No live packaged-model success is claimed.
- Under the user's existing authorization, Windows Search, its WebView children, and Settings were closed for headroom. The OS may restart these components. Codex/ChatGPT, security services, and core Windows services were left running.
- All **947 original historical files** remain unchanged. All **739 files in the previous completion receipt** remain available either unchanged or through verified original-source/engine archives. Previous receipts, reports, answers, rejected configurations, and failed attempts were retained.

## Evidence index

Paths are relative to `AI-PowerBI-Analyst/.runtime/`. Each artifact run has a frozen task copy, request evidence, answers, and `quality-review.json`.

| Evidence | Directory |
|---|---|
| Initial control / v1 | `qwen-quality-baseline-1880d863842b4142ad2d1138fdee8179` / `qwen-quality-candidate-403b9405b8e34fec9731c5947a98718b` |
| v2 H holdout and replays | `qwen-quality-candidate-3cb89607c81540898f80be00ad7206dc` |
| H control | `qwen-quality-baseline-7b8b631caf09461a928fdb6f1afda607` |
| N control / rejected v3 | `qwen-quality-baseline-7c1c1e463a514a6c8eb90e0c8c223402` / `qwen-quality-candidate-c0a4a7aa3e954886be3534e7c04c8299` |
| Final factory candidate / control | `qwen-quality-production-1a73e663b08f48a9997eab18adf12baf` / `qwen-quality-factory-control-fdfd4f4e08d8432da2c44b58f3675655` |
| Name-parser replay | `qwen-quality-candidate-6991b3abc0cf4c44846edeaf3eba669d` |
| Packaged guard checks | `qwen-quality-package-93f59db81aaa4e359968339743bebf32` / `qwen-quality-package-837757cb9ce2484c9e109867cfaf7770` |
| Analysis, test/build logs, archives, preservation mapping, final receipt | `qwen-quality-20260917` |

Experiment scripts and frozen payloads are under `evals/qwen_quality_20260917/`. Historical configurations pin old source bytes; use the archived-source mappings when reproducing those runs.
