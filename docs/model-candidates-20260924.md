# Small-model candidates vs the Qwen 4B champion — 24–25 September 2026

**Keep Qwen3.5 4B.** Across both suites it is the most accurate model: 46 of
59 tasks, against 34 for Gemma 4 E2B and 35 for Granite 4.2 3B.

- **App suite (27 tasks):** Gemma E2B led narrowly, 22/27 against 20/27, and
  generated 1.7× faster.
- **Historical artifact suites (32 tasks):** the order reversed. Qwen scored
  26/32, Gemma 12/32 and Granite 15/32; see "Historical artifact suites" below.
- **Gemma's weaknesses:** it failed every Power Query M task and most
  relationship tasks.
- **Speed:** Gemma remains the fastest Standard-class model.
- **Other arms:**
  - Qwen3.5 2B re-confirmed its rejection.
  - Gemma 4 E4B does not fit this laptop's free memory.

## What ran

Every model ran through the app's real code paths (`StudioService.chat_events`,
`dax_assistant_events`, `create_draft`, `revise_draft`) on a private copy of the
Superstore (2) workspace (Orders, Returns, People, two approved joins). Only
`service.model` changed. Prompts, retrieval, schemas, validators, repairs,
deterministic completions and timeouts were the app's own.

- **Same engine.** Every model used Ollama 0.33.3's bundled `llama-server.exe`
  with production's flags: 4 threads, 4,096 context, q8 KV cache, flash
  attention, 128 MiB prompt cache, a 6 GiB job cap, greedy decoding and seed
  9162026.
  - The champion used the unmodified production `TextOnlyServer` and its pinned
    Standard profile.
  - The Coder 1.5B used its real Light profile (managed Ollama).
  - Candidates used `--jinja`, which applies each model's own chat template
    with thinking disabled. All other settings were identical.
- **Frozen tasks.** 27 tasks were written before any model ran
  (`evals/candidates_20260924/tasks.json`):
  - 6 chat questions with hallucination traps;
  - 13 DAX Lab formulas, with answers computed from the source workbook in
    pandas (`truth.py`);
  - 3 explain/advice turns;
  - 2 new drafts;
  - 3 revisions, including the user's real "year over year matirx" request.
- **Scoring.**
  - DAX results were evaluated by the app's engine and compared with the
    pandas truth.
  - Chat and explanations were read in full (`review.json`). Every regex
    verdict that was overridden records its reason.
  - One harness fault (X3 mode) was fixed after the first champion run. That
    task was excluded from run 1; run 2 includes it.
- **Repeatability.** The champion ran twice and gave identical answers, so the
  suite is deterministic.

## Results

| Model | Score (reviewed) | Chat | DAX + explain | Drafts | Revisions | Tokens/s | Median task | Peak RAM | Suite time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Qwen3.5 4B (champion, run 2) | 20/27 | 6/6 | 12/16 | 2/2 | 0/3 | 7.0 | 24.4 s | 2.9 GiB | 14.6 min |
| **Gemma 4 E2B** | **22/27** | 6/6 | **14/16** | 2/2 | 0/3 | **11.7** | **17.9 s** | 3.5 GiB | **10.7 min** |
| Granite 4.2 3B | 20/27 | 5/6 | 13/16 | 2/2 | 0/3 | 8.3 | 25.1 s | 2.5 GiB | 14.9 min |
| Qwen2.5-Coder 1.5B (Light) | 14/27 | 6/6 | 8/16 | 0/2 | 0/3 | 18.6 | 8.5 s | 1.2 GiB | 3.5 min |
| Qwen3.5 2B | 11/27 | 3/6 | 8/16 | 0/2 | 0/3 | 14.5 | 15.6 s | 1.5 GiB | 8.9 min |
| Gemma 4 E4B | not run | | | | | | | >4.7 GiB | |

Gemma 4 E4B tripped the 1 GiB free-RAM floor while loading, with 5.7 GiB
available alongside the user's normal apps. Its 5.0 GB model file needs about
5.5 GB resident against the champion's 3 GB.

| Task | Qwen 4B | Gemma E2B | Granite 3B | Coder 1.5B | Qwen 2B |
|---|---|---|---|---|---|
| D05 profit on returned orders | fail (plain `SUM`, accepted) | fail | fail | fail | fail |
| D10 Consumer share of sales | fail (`DIVIDE(x,x)`=1) | fail (same) | fail (same) | fail | **pass** |
| D12 discount above 20% | fail (`> 20`) | pass | pass | fail | fail |
| D13 sales by region, highest first | fail (sorted by name) | **pass** | fail (by name) | pass | fail |
| Revisions finishing inside 90 s | 0/3 (all time out) | **3/3** (rejected, see below) | 1/3 | 0/3 (never sent) | 3/3 (rejected) |

## Historical artifact suites (32 tasks, 25 September)

Each model ran 32 frozen tasks from earlier qualifications through the app's
production contract runtime, with retrieval, artifact validators and one
repair:

- head-to-head v3 holdout + confirmation (16);
- adaptive final + coder holdout (16).

SQL was executed in DuckDB with each suite's original rule. The 50 DAX, M and
relationship answers were graded blind by three independent judges, with model
names hidden and a majority verdict. Only one item split the vote.

| Model | Total | DAX | SQL | Power Query M | Relationships | Median task |
|---|---:|---:|---:|---:|---:|---:|
| **Qwen3.5 4B** | **26/32** | 8/8 | 7/8 | 4/8 | 7/8 | 36.5 s |
| Granite 4.2 3B | 15/32 | 5/8 | 4/8 | 0/8 | 6/8 | 24.2 s |
| Gemma 4 E2B | 12/32 | 5/8 | 5/8 | 0/8 | 2/8 | 16.4 s |

- **Gemma.**
  - Its M answers failed Microsoft's M parser even after the one repair.
  - Its relationship answers stated 1:1 for repeating keys, which the app's
    relationship validator rejects.
  - Its DAX misses include USERELATIONSHIP replaced by RELATEDTABLE, and a
    missing zero alternate result.
- **Granite.** Relationships are solid, but M syntax, SQL `CREATE`/typed-schema
  echoes and "Measure:" prefixes fail validation.
- **Caveat.** These task families were used while tuning the app's retrieval
  and validators around Qwen, so they favour it somewhat. The margin (26 vs
  12–15) and the kind of failure (syntax errors, wrong cardinality) are too
  large to be explained by that alone.

A first candidate pass through the OpenAI-style chat endpoint was invalid.
With a JSON schema, llama-server's Gemma handler admitted a "Thinking Process:"
preamble, and every schema-bound DAX/M answer failed with "Unexpected thinking
output". The harness was switched to production's native `/completion` +
`json_schema` path and the candidates were re-run. The invalid runs are kept as
`*-chatapi-invalid`. The earlier 27-task suite was checked and had no thinking
failures.

## Findings about each model

- **Qwen3.5 4B.**
  - Solid chat, and draft narratives are correct.
  - On this CPU every revision times out at 90 s, and prefill alone takes about
    50 s for the roughly 1,500-token revision prompt.
  - In explain mode it replies with a raw JSON object.
- **Gemma 4 E2B.**
  - Best DAX: the only model to sort D13 by value, and it got D12's 0.2
    threshold right.
  - Fastest of the Standard-class models; all revisions finished (66–80 s).
  - Every revision proposal was rejected because it repeats a new page's visual
    in `visual_additions` pointing at that new page ("Visual addition must
    target an existing page ID").
  - Chat quirks:
    - writes fields as `Table[Orders][Region]`;
    - prefixes formula explanations with a JSON block.
- **Granite 4.2 3B.**
  - Clean prose explanations.
  - Most direct first-try formulas (7/13).
  - Its shipping answer dumped 17 fields, three of them repeated.
  - Revisions time out like the champion's.
- **Qwen3.5 2B.**
  - Five DAX rejections from invalid `SUMMARIZECOLUMNS` use.
  - Misstated the schema: said People holds only Person, and called Return Rate
    a People metric.
  - Both draft caveats claimed joins, which the prompt forbids.
  - Confirms the 18 September rejection.
- **Coder 1.5B (Light).**
  - Very fast.
  - Chat is short and correct.
  - Half of its DAX is invalid.
  - Both narratives failed: one ran past the token limit, the other ignored the
    40-word limit.
  - Revisions are never attempted: the 5.8 KB revision prompt exceeds the Light
    backend's byte bound.

## App issues found (independent of the model)

1. **D05:** the DAX Lab accepted `SUM('Orders'[Profit])` for "profit on orders
   that were returned". `_request_gap` does not notice the missing Returned
   condition. No model wrote `CALCULATE(..., 'Returns'[Returned] = TRUE())`.
2. **D10:** `CALCULATE(DIVIDE(x, x), filter)` (always 1) passes validation for a
   share-of-total request. Three of five models produced it.
3. **D13:** "highest first" is not checked, so region-name ordering passes
   validation.
4. **R2:** the user's "create a year over year matirx at the bottom" failed for
   every model with "The governed formula request must name a reviewed field or
   a supported margin".
5. **R1:** the fallback revision built "Profit by Category" when the user asked
   for sub-category.
6. **References footer:** chat answers still carry irrelevant references, for
   example ArcGIS maps under the age/gender answer and `Table.RemoveLastN` under
   the table overview.
7. **Duplicate visual additions:** every revision that finished in time from
   Gemma (3/3) and Granite (1/1) was rejected for the same reason. The model
   also lists the new page's visual under `visual_additions`, targeting that
   new page. Two model families doing this points to the schema or prompt.
   Dropping an addition that duplicates a visual on a newly proposed page
   would be a safe normalization. Qwen 2B's revisions were rejected for
   different reasons: invented fields, and restating a measure.

## Recommendation and next steps

- Keep Qwen3.5 4B. Gemma 4 E2B's 27-task lead did not survive the historical
  suites. Its speed is attractive, but it cannot be trusted with Power Query M
  or relationships.
- Revisit Gemma only if the chat artifact path stops being a product feature,
  or after M/relationship-specific constraints are added.
- Gemma 4 E4B could only be tested with more free RAM, or with its per-layer
  embedding tables left memory-mapped (untested).
- Separately, fixing app issues 1–4 and 7 improves every model, including the
  current champion.
- Keep Coder 1.5B as the Light profile. Qwen3.5 2B is worse on these tasks, and
  Granite's 2.5 GiB peak sits at Light's 2.5 GiB process cap.

## Evidence

- Harness, tasks, truth, review and scores: `evals/candidates_20260924/`
  (`backend.py`, `run.py`, `tasks.json`, `truth.json`, `review.json`,
  `scores.json`).
- Per-run state copies, per-task results and every prompt/response:
  `.runtime/candidates-20260924/<run>/` (`results/*.json`, `model-evidence/`).
- Downloads, hash-verified against the publishers' SHA-256:
  - `.runtime/model-candidates-20260924/gemma-4-E2B-it-Q4_K_M.gguf`, sha256 `740185b2…`;
  - `gemma-4-E4B-it-Q4_K_M.gguf`, sha256 `85a896a0…` (unsloth, Apache-2.0);
  - `granite4.2:3b` in the Ollama store, blob `20e43614…`.
- Host: HP EliteBook 840 G8, i7-1165G7, 16 GB (about 5.7 GiB free during
  runs), Windows 11, CPU only.
