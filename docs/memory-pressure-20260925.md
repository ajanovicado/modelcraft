# Busy-laptop memory test: Qwen 4B vs Qwen 2B vs Coder 1.5B — 25 September 2026

**Keep Qwen3.5 4B as the only model.** On a busy laptop, answers stayed correct and kept their normal speed:
- on a 16 GB laptop, down to 0.5 GB free RAM;
- on a simulated 8 GB laptop, down to 1 GB free.

The costs of low memory:
- loading takes about 16 s instead of 6 s;
- other apps can stall for a few seconds while Windows moves their idle memory to the page file;
- the page file grows by 1–4 GB.

The smaller models are 2–2.5× faster and use less than half the memory. But with today's fixes they score 15/27 (2B) and 14/27 (1.5B), against Qwen 4B's 23/27. They fail about half the DAX formulas outright, and both dashboard drafts.

This supersedes the conclusion of `qwen-memory-test-20260921.md`. That test stopped Qwen 4B during load because of the app's own free-memory floors, which were removed on 2026-09-25.

## Machine

- Intel Core i7-1165G7 (4 cores, 8 threads) and 16 GB RAM (15.3 GiB usable).
- Samsung 990 PRO NVMe.
- System-managed page file: 2.3 GB at the start, so a commit limit of 17.6 GB.

This is a typical business laptop, except for its unusually fast SSD. A slower SSD will lengthen load-time paging and app stalls. It should not change per-answer speed, because the model's pages stay in RAM once loaded.

## Method

`evals/candidates_20260924/pressure.py` runs four app tasks through the real service code, with the app's own prompts, validators and timeouts:
- C1, a chat question;
- D10, the Consumer share formula;
- D13, sales by region, highest first;
- X2, explaining a formula.

While they run, `hog.py` simulates other apps. It holds random, incompressible memory until only the target amount is free. Half of that memory is rewritten every second (apps in use); the other half is idle (minimised apps). The first version held near-empty pages, which Windows memory compression squeezed to nothing, so those results were discarded (`.runtime/pressure-20260925/compressible-v1`).

- **"8 GB laptop"** (`8:<free>`): RAM above 8 GiB is first held and rewritten every second, so it can never make room for the model.
- **Engine and settings:** production's own engine and flags throughout: `--load-mode none --repack`, 4 threads, 4,096 context.
- **Harness floor:** no free-memory floor, matching the app.

## Results (seconds per task; every answer checked against ground truth)

| Model | Free RAM at start | Load | Chat | DAX 1 | DAX 2 | Explain | Other apps: longest stall | Page file growth |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| Qwen 4B | ~9 GB (no pressure) | 6 | 37 | 25 | 26 | 24 | – | 0 |
| Qwen 4B | 2 GB | 12 | 45 | 24 | 24 | 22 | 0.6 s | 3.2 GB |
| Qwen 4B | 1 GB | 16 | 48 | 24 | 26 | 22 | 3.8 s | 3.6 GB |
| Qwen 4B | 0.5 GB | 16 | 47 | 23 | 26 | 23 | 2.1 s | 4.1 GB |
| Qwen 4B | 8 GB laptop, 2 GB | 12 | 40 | 23 | 26 | 23 | 0.9 s | 0.5 GB |
| Qwen 4B | 8 GB laptop, 1 GB | 17 | 50 | 22 | 25 | 24 | 7.0 s (once) | 1.2 GB |
| Qwen 2B | ~10 GB (no pressure) | 4 | 17 | 11 | 13 | 10 | – | 0 |
| Qwen 2B | 0.5 GB | 8 | 29 | 11 | 13 | 9 | 4.6 s | 4.1 GB |
| Qwen 2B | 8 GB laptop, 1 GB | 8 | 25 | 10 | 13 | 10 | 3.1 s | 1.1 GB |
| Coder 1.5B | ~10 GB (no pressure) | 2 | 13 | 10 | 11 | 8 | – | 0 |
| Coder 1.5B | 0.5 GB | 5 | 21 | 9 | 12 | 8 | 2.3 s | 3.8 GB |
| Coder 1.5B | 8 GB laptop, 1 GB | 4 | 18 | 11 | 13 | 7 | 1.2 s | 1.7 GB |

Every answer in every run was correct. The "longest stall" column is the slowest one-second rewrite of the simulated in-use apps. Its median stayed at 15–110 ms throughout.

Resident memory while answering:
- Qwen 4B: 2.7–2.8 GiB (3.3 GiB committed);
- Qwen 2B: 1.4–1.5 GiB;
- Coder 1.5B: 1.1 GiB.

## Memory-mapped loading does not help this model

`--load-mode mmap --no-repack` was measured on the same tasks, with these results:
- Qwen 4B still held 2.7–2.8 GiB as private memory (USS ≈ RSS), and the same 3.3 GiB was committed.
- Prompt reading ran about 1.8× slower without the repacked CPU layout, e.g. D10 took 48–54 s instead of 24–25 s.

The loader converts Qwen's text tensors at load time, as recorded in `qwen-memory-test-20260921.md`. So the weights end up private either way. Production keeps `none` + `repack`. `TextOnlyServer` now reads `load_mode` and `repack` from the profile (defaults unchanged), so this can be re-tested on a future engine.

## Quality with today's fixes (27-task app suite, reviewed)

| Model | Score | Chat | DAX + explain | Drafts | Revisions | Median task | Peak RAM |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Qwen 4B** | **24/27** | 6/6 | 15/16 | 2/2 | 1/3 | 25 s | 3.0 GiB |
| Qwen 2B | 15/27 | 6/6 | 9/16 | 0/2 | 0/3 | 16 s | 1.5 GiB |
| Coder 1.5B | 14/27 | 6/6 | 8/16 | 0/2 | 0/3 | 8 s | 1.2 GiB |

Before today's fixes the scores were 20/27, 11/27 and 14/27.

**Small models.** Most of their misses are formulas that fail local validation even after the one correction, so the user gets no answer:
- the 2B writes invalid SUMMARIZECOLUMNS;
- the Coder puts values in single quotes.

Their dashboard narratives also fail review.

**Qwen 4B's misses:**
- **D12:** compares Discount to 20 instead of 0.2.
- **R2:** "year over year matrix" is refused by design; the matrix visual does not exist yet.
- **R1:** a correct page used to be rejected because the model also put the Orders table ID into the relationship list. Since 2026-09-25 that field is emptied when no join was offered. The re-run (`qwen35-4b-postfix2`) accepted the page: 24/27, and the first model revision this suite has accepted.
- **R3:** the answer drew "trend" lines over Ship Mode, which is not a date, and was rightly rejected; the fallback built the page.

Evidence:
- runs: `.runtime/candidates-20260924/*-postfix`;
- memory arms: `.runtime/pressure-20260925/`;
- reviewed verdicts: `evals/candidates_20260924/review.json`.

## Risks this test cannot rule out

- **A fixed, small page file.** If IT pins the page file to a small, fixed size, Windows cannot move idle memory aside. Qwen 4B could then fail to load on a busy machine instead of slowing down. Windows' default is system-managed.
- **Slower SSDs.** Load-time paging will take longer than the 12–17 s measured here.
- **Two heavy sessions at once.** The app unloads the model after 120 s idle, which gives the 2.8 GB back. Running Power BI Desktop refreshes while Qwen answers was not measured.

## Downloading from Hugging Face instead of Ollama's registry

No public Hugging Face file matches the app's pinned model byte for byte. Qwen publishes only safetensors for Qwen3.5-4B, and community GGUF builds differ from Ollama's.

The most-used build, `unsloth/Qwen3.5-4B-GGUF` `Qwen3.5-4B-Q4_K_M.gguf`, is 2.74 GB against 3.39 GB, pinned at commit `e87f176479d0`. The download:
- ran through the app's own installer code (`llm/model_install.py`), at 43.7 MB/s;
- was verified by SHA-256;
- loaded in the production engine.

The extra 0.65 GB in Ollama's file is weights the app never loads, so RAM use was the same, 2.8–2.9 GB.

On the same 27 tasks, with the same prompts, engine and flags, it was worse:

| Qwen 4B file | Score | DAX | Median task | Time-outs |
|---|---:|---:|---:|---:|
| **Ollama build (pinned)** | **24/27** | 15/16 | **25 s** | 0 |
| unsloth Q4_K_M | 21/27 | 13/16 | 35 s | 2 |

It wrote invalid DAX for D03 and D07 (`SUM(...) FILTER ...`) that the pinned build answers correctly. Both page revisions also hit the 90 s deadline. Decoding is greedy, so these differences come from the weights, not from timing.

The pinned build stays. For a Hugging Face download, upload that exact file (Apache-2.0 permits redistribution with the license) to a repository the project controls, and point `model_download.url` at it. No re-validation is needed, because the SHA-256 is unchanged.

## Speed levers measured on this CPU

- **Multi-token-prediction drafts** (`--spec-type draft-mtp`, using the MTP layers the pinned file carries): slower, at 5.0 vs 6.1 tokens/s, and used 0.35 GB more RAM. Answers were identical.
- **N-gram drafts** (`--spec-type ngram-mod`): no change on a short answer (6.2 vs 6.1 tokens/s). Suite result below.
- **Memory-mapped loading:** see above; 1.8× slower prompt reading and no memory benefit.

`TextOnlyServer` reads `spec_type` from the profile. It is off unless a profile names it.

## Instruct-only alternatives (same 27 tasks, reviewed)

Qwen publishes no instruct-only (non-thinking) Qwen3.5 small models. The 0.8B, 2B, 4B and 9B are hybrid, and the app turns thinking off. The closest options tested:
- **Qwen3-4B-Instruct-2507**, Qwen's previous-generation non-thinking 4B, as `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB, Apache-2.0).
- **Phi-4-mini** (3.8B, MIT), which appears in earlier plans as a fallback.

Both ran as candidates on the production engine with their own chat templates.

| Model | Score | Chat | DAX + explain | Drafts | Revisions | Median task | Peak RAM |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Qwen3.5 4B (pinned)** | **24/27** | 6/6 | 15/16 | 2/2 | 1/3 | 25 s | 3.0 GiB |
| Qwen3 4B Instruct 2507 | 20/27 | 6/6 | 12/16 | 2/2 | 0/3 | 23 s | 2.9 GiB |
| Phi-4-mini 3.8B | 14/27 | 5/6 | 7/16 | 1/2 | 1/3 | 23 s | 2.7 GiB |

**Qwen3 Instruct 2507.** It is as fast and as light as Qwen3.5 4B, but less accurate:
- D09 counted order lines with `COUNTROWS(FILTER(...))`, 543 instead of 264 orders.
- D08 failed validation.
- X3 asked a clarifying question instead of answering.
- Neither page revision satisfied its request.

**Phi-4-mini** failed most DAX, using unsupported SUMX, string SUMs and invalid DIVIDE forms. It also answered C4 with unrelated SQL.

Neither replaces Qwen3.5 4B.
