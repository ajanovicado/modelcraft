# Qwen versus MiniCPM with the current RAG runtime

Measured on this Windows laptop on 17 September 2026. **Keep Qwen as the production model.** On eight fresh paired tasks, Qwen produced seven correct artifacts and MiniCPM produced one. MiniCPM decoded faster, but repairs and recovery reloads left its median request time only about 5% lower.

| Measurement | Qwen3.5 4B Q4_K_M | MiniCPM5-2B Q4_K_M |
|---|---:|---:|
| Correct artifacts on eight fresh tasks | **7/8** | **1/8** |
| Artifacts returned / blocked | 8 / 0 | 2 / 6 |
| Median time to answer or failure, warm session enabled | **20.34 s** | **19.30 s** |
| Mean time to answer or failure | 29.58 s | 18.97 s |
| Longest request | 91.19 s, incorrect M | 32.59 s, incorrect SQL |
| Median initial process startup and model load | 10.09 s | 5.08 s |
| Median internal first-content time | 11.94 s | 7.80 s |
| Decode rate, completed streams only | 6.51 tokens/s | 12.49 tokens/s |
| Prompt processing, completed streams only | 36.53 tokens/s | 61.26 tokens/s |
| Requests needing a repair | 1/8 | 7/8 |
| Model process loads, including recovery | 2 | 6 |
| Peak model-tree resident RAM | 4.20 GiB | 1.82 GiB |
| Peak model-tree private memory | 4.95 GiB | 1.98 GiB |

The latency figures include retrieval, generation, validation, repairs, and any recovery reload. Two planned initial loads per model are reported separately. MiniCPM's six blocked requests count as failures, not fast answers. Qwen spent 236.67 seconds on requests and MiniCPM 151.75 seconds; adding the planned initial loads gives 256.86 and 161.91 seconds, respectively. These totals exclude final unload time and independent post-run judging.

Only one task was correct for both models: the active relationship took 24.09 seconds for Qwen and 16.91 seconds for MiniCPM. That single pair is insufficient to establish a general speed advantage for correct answers. Different tokenizers also make tokens/s a diagnostic rather than a like-for-like useful-output metric.

## Fairness and scope

- Eight prompts were written and hash-frozen before inference: two each for DAX, SQL, Power Query M, and relationships. The expected answers and SQL fixtures were never sent to the models.
- Both models used the **same current `ContractOllamaModelRuntime`**, original top-two lexical RAG, identical initial messages and response schemas, deterministic validation, and at most one repair. All eight initial prompt hashes and retrieved-example IDs match across models.
- Four-task block order was Qwen then MiniCPM, followed by MiniCPM then Qwen. Each block began with an explicit load but no priming question or task output. Models were never loaded concurrently.
- Both used the same hash-pinned Ollama 0.33.3 / llama.cpp runtime: four CPU threads, GPU off, 4,096-token context, 512-token output cap, greedy decoding, thinking off, seed 9162026, and a 512 MiB saved-prompt cache. Flash attention was off and KV cache was f16.
- Guards remained unchanged: AC, 8 GiB free RAM/disk at startup, 3 GiB while running, 512 MiB new-paging cap, 6 GiB process/job cap, one-hour session cap, exclusive lock, stop-file handling, and 60 seconds per inference attempt.
- Qwen's package digest is `2a654d98e6fba55d452b7043684e9b57a947e393bbffa62485a7aac05ee4eefd`. The temporary MiniCPM package exactly reproduced the old digest `442cd6a5d6a9da8602076864fbd98642e68b4ab484c366195b17785c8921d8f1`, including its template and parameters. Its GGUF SHA-256 is `ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd`, downloaded from the [publisher's GGUF repository](https://huggingface.co/openbmb/MiniCPM5-2B-GGUF) at revision `2079a22f3beaa4e306449978533478fe0522f4b3`.
- The commercial names are 4B and 2B; the installed package metadata reports 4.7B and 2.5B. This is a comparison of those exact packages, not all Qwen or MiniCPM variants. Publisher GGUF equivalence to the earlier Mac BF16 revision remains unproven.
- One run per task/model, on one laptop; OS file cache and background OS activity were not controlled. This is a small measured comparison, not a broad accuracy estimate or statistical speed guarantee. It does not supersede the separate Mac cleanroom experiment.

Warm sessions normally remain loaded, but the existing runtime unloads after a terminal validation error. Consequently, MiniCPM had four recovery loads. Their time is included in its request timings. This is the actual current product behavior, not an isolated kernel benchmark. Internal first-content timing excludes those loads and is **not** time until the user sees a validated artifact.

## Individual tasks

| Task | Qwen seconds | Qwen correct | MiniCPM seconds | MiniCPM correct |
|---|---:|:---:|---:|:---:|
| P01 — DAX ratio of totals | 16.06 | Yes | 9.38 | No, blocked |
| P02 — active dimension/fact relationship | 24.09 | Yes | 16.91 | Yes |
| P03 — filtered SQL grouped sum | 14.77 | Yes | 16.00 | No, blocked |
| P04 — M null/filter/sort | 33.28 | Yes | 21.70 | No, blocked |
| P05 — DAX SUMX revenue | 16.61 | Yes | 9.38 | No, blocked |
| P06 — inactive date relationship | 22.27 | Yes | 21.89 | No, blocked |
| P07 — SQL clients without open tickets | 18.41 | Yes | 32.59 | No, returned wrong rows |
| P08 — M grouped sum | 91.19 | No, returned invalid function arguments | 23.91 | No, blocked |

SQL was executed by the unchanged independent DuckDB judge against fresh fixtures, including duplicates, ties, and a null key. DAX and relationships received static checks plus manual semantic review. M received the Microsoft parser plus manual review; native Power BI/M execution was not available. Parser success alone did not earn a correct score.

## What to improve next

1. **Retain Qwen and its warm runtime.** MiniCPM is smaller and roughly 1.9 times faster at decoding completed streams, but only one of eight tasks produced a correct artifact. It is not a useful replacement on this evidence. No larger Qwen model was tested here.
2. **Test shorter, better-matched retrieval on a new holdout.** P08 received two DAX examples for an M grouping request; P04 received a relationship example and an M merge example. The measured Qwen wait before first content was about 12 seconds median, so prompt processing is a concrete optimization target. This does not prove retrieval caused the errors or establish a speedup from changing it. The earlier one-example/language-aware policy regressed; do not reinstate it without a new controlled ablation.
3. **Address repetitive generation and M semantics before increasing model size.** Qwen P08 repeated a malformed fragment until the unchanged 60-second watchdog stopped attempt one. Its repair took another 30.73 seconds and passed syntax checks but used invalid `Table.Group` arguments. An early repetition detector and targeted M argument checks are candidates for a fresh test, with the existing resource guards retained. Microsoft's [Table.Group signature](https://learn.microsoft.com/en-us/powerquery-m/table-group) requires a numeric or null fourth argument; the generated code supplied a list. Both attempts and the failure remain in the evidence.

The timed-out Qwen stream has partial text but no final native token counts. Decode and prefill rates above use completed native timing records only; the full timeout remains included in request latency. No speed or quality improvement from the recommended changes has been claimed or deployed.

## Evidence and cleanup

The production configuration and packaged application remain unchanged. No training, corpus edits, validator edits, or guard changes were made. The temporary MiniCPM weights were removed after the comparison; their source, hashes, package metadata, prompts, raw answers, logs, and reviews are preserved. Qwen remains the only production model.

- [Frozen protocol and hashes](../evals/rag_speed_h2h_20260917/manifest.json)
- [Paired results and timing analysis](../.runtime/rag-speed-paired-20593c62429641b68b40d1fb761f413b/analysis.json)
- [Independent checks and semantic reviews](../.runtime/rag-speed-paired-20593c62429641b68b40d1fb761f413b/quality-review.json)
- [Verification before cleanup](../.runtime/rag-speed-h2h-20260917/pre-cleanup-verification.json): all 72 frozen pins verified; all 546 files from the preceding quality receipt unchanged; no remaining model workers.
- [Cleanup receipt](../.runtime/rag-speed-h2h-20260917/cleanup.json)
- [Final receipt](../.runtime/rag-speed-h2h-20260917/final-receipt.json)
