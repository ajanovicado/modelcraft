# Qwen storage and memory investigation — 17 September 2026

The most promising hardware change is **32 GB as two matched 16 GB DDR4-3200 SODIMMs**, not another SSD. The laptop currently has one Samsung 16 GB module and a Samsung 990 PRO 1 TB NVMe SSD. More RAM would provide headroom; using both memory channels could improve inference bandwidth. That speed improvement has **not** been measured. No hardware purchase, Windows memory setting, model file, or production runtime setting was changed.

## Hardware and the SSD/RAM distinction

Windows reports an HP EliteBook 840 G8, i7-1165G7 with four physical cores/eight logical processors, and one populated memory module: Samsung `M471A2G43AB2-CWE`, configured at 3200 MT/s. Firmware reports two memory slots. HP documents dual-channel support and a 2 x 16 GB configuration for this model. A compatible second 16 GB module is the first upgrade to investigate; verify module compatibility before buying. The firmware capacity field reports 32 GB while HP's product family specifications list up to 64 GB; this recommendation stays at 32 GB. [HP specifications](https://support.hp.com/my-en/document/c06978058)

Think of the SSD as the storage shelf and RAM as the workbench. Windows can move less-used memory pages to the SSD's pagefile, which increases available committed-memory capacity. This does not make SSD accesses operate at RAM speed. For this resident Qwen model, deliberately spilling active model data to disk is not a speed optimization. The existing pagefile was 2560 MiB, with 74 MiB in use at inspection; it was left unchanged. [Microsoft pagefile documentation](https://learn.microsoft.com/en-us/troubleshoot/windows-client/performance/introduction-to-the-page-file)

Memory mapping connects a model file to virtual memory, letting the OS manage its resident pages. It can affect loading and memory accounting; it does not create faster RAM. llama.cpp offers mmap, ordinary loading, locked-memory and direct-I/O modes. The installed executable's help confirmed these options. [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

## Fresh guarded experiment

Compared the current Qwen configuration against a benchmark-only `use_mmap=true` request. All other settings were identical: model digest, corpus, retrieval, prompt construction, validators, four CPU threads, 4096 context, greedy inference, thinking disabled, 512 output tokens and one allowed repair. The installed Ollama release exposes the option when constructing its llama-server command. [Ollama 0.33.3 source](https://github.com/ollama/ollama/blob/v0.33.3/llm/llama_server.go)

Four previously unused tasks covered SQL, DAX, Power Query M and a relationship. Both settings received identical prompts and retrieved examples. Order was baseline/candidate for the first two tasks, then candidate/baseline for the other two. Each block had a newly loaded process and a warm session for its two requests. Mandatory model hashing reads the weights before loading; this is not a cold-SSD bandwidth benchmark.

| Measurement | Current setting | mmap requested |
|---|---:|---:|
| Correct artifacts | 4/4 | 4/4 |
| Repairs | 0 | 0 |
| Median warm request | 18.914 s | 19.164 s |
| Median process startup, including attestation | 8.773 s | 8.868 s |
| Aggregate decode rate | 6.635 tokens/s | 6.652 tokens/s |
| Peak sampled process-tree RSS | 3.827 GiB | 3.833 GiB |
| Peak sampled private memory | 4.509 GiB | 4.514 GiB |

**The candidate did not actually enable memory mapping.** All four native runner logs report `compat patch disabled mmap for transformed text tensors`, followed by `load_mode = none`. There is consequently no demonstrated mmap benefit here, and the small timing differences must not be attributed to a different loading mode. Production stays unchanged.

All four output pairs were byte-identical. SQL executed against a fixture with a filter, aggregation and tied totals. DAX, M and relationship artifacts passed the production checks and explicit static/manual semantic review; they were not executed inside Power BI or Power Query. This small pilot is not a broad correctness qualification.

Every existing guard remained active: 8 GiB startup RAM/disk plus AC, 3 GiB while running, 512 MiB new paging, 6 GiB process/job memory, one-hour cap, exclusive lock, stop file and generation deadlines. Paging counters did not increase across the recorded samples. All owned workers exited. Sixty runtime/model pins plus four benchmark/configuration files verified unchanged afterward.

## Recommended order

1. **Investigate the second compatible 16 GB RAM module.** The added channel is the potential speed improvement; added capacity also helps the app pass its unchanged startup guard. Benchmark the same configuration after installation using new tasks. Do not assume twice the RAM means twice the token rate.
2. **Keep the existing warm-session reuse.** Earlier paired SQL evidence measured a median request reduction from 24.797 s to 14.891 s. That is an existing optimization, not a new result from this experiment. Including model startup reduced the total improvement in that particular workload to about 19.7%. Keeping a model loaded avoids repeated loading work. [Ollama documentation](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately)
3. **Prioritize RAG relevance and compact prompts next.** The new M filtering task retrieved relationship/DAX examples, and the SQL aggregation task retrieved join examples. They succeeded, but the examples were poorly matched. Current internal first-content time was about 10.2 s median; this includes prompt processing and is not visible streaming latency because artifacts are buffered for validation. Test domain-aware selection and shorter relevant examples with new quality holdouts. A previous one-example retrieval change regressed quality, so no untested replacement was enabled.
4. **Treat thread count and integrated-GPU changes as experiments.** Four threads already match this CPU's physical cores. A three-thread pilot is a reasonable later check, not a promised gain; llama.cpp advises measuring rather than oversubscribing threads. Previous integrated-GPU trials were slower and one hit the memory floor. A RAM upgrade could justify retesting GPU behavior, but does not establish a win. [llama.cpp performance guidance](https://github.com/ggml-org/llama.cpp/blob/master/docs/development/token_generation_performance_tips.md)
5. **Keep the current model size for the speed objective.** A larger dense model is not a justified speed upgrade, and forcing it into SSD-backed memory conflicts with keeping this workload resident and within the existing resource limits. Do not create a RAM disk, enlarge the pagefile as a speed tweak, or disable guards. A RAM disk would consume the scarce memory the app needs.

## Reproducible evidence

- Benchmark: `evals/qwen_storage_20260917/run.py`; frozen fresh tasks: `tasks.json`; analysis: `analyze.py` in the same directory.
- Results: `.runtime/qwen-storage-a01655c290a34c4a8a13fd4db60d02b4/` contains the pre-inference manifest, configuration, raw answers, attempt receipts, native logs, hardware snapshot, quality review and analysis.
- `analysis.json` records prompt/retrieval/output equality, quality methods, all verified hashes and the ineffective-mmap finding. A later create-once `analysis-*.json` adds exact checks against the manually reviewed M/model artifacts without rerunning inference.
- The executed harness and initial analyzer were preserved byte-for-byte as `frozen-run.py` and `frozen-analyze.py` before formatting the maintained scripts. The original harness still matches its pre-inference manifest hash. Ruff passes for the maintained scripts.
- Earlier production improvements: `docs/qwen-speed-20260916.md`; latest broader quality limitations: `docs/rag-speed-head-to-head-20260917.md`.
- All files above are additions; historic answers, reports and receipts were preserved. These four tasks have now been consumed. Future qualification requires fresh holdouts.
