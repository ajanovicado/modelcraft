# Qwen memory experiment — 21 September 2026

Follow-up: `qwen-text-only-test-20260921.md` documents successful text-only 4B trials, including confirmed mmap with repacking disabled. The results below apply to the original multimodal loading path.

The installed Qwen 3.5 4B package did not pass a bounded low-memory test with Power BI Desktop and Studio open. Requesting SSD-backed memory mapping did not enable it: the native loader explicitly disabled mmap for transformed Qwen text tensors. Production model profiles and Windows pagefile settings were not changed.

## Measured results

| Variant | Free RAM at start | Lowest sampled free RAM | Result |
| --- | ---: | ---: | --- |
| Qwen2.5-Coder 1.5B, F16 cache | 3.32 GiB | 2.28 GiB | Completed four requests |
| Qwen3.5 4B, F16 cache | 3.48 GiB | 1.25 GiB | Stopped during load |
| Qwen3.5 4B, Q8 cache + Flash Attention | 3.78 GiB | 1.14 GiB | Stopped during load |
| Qwen3.5 4B, Q8 + explicit `use_mmap=true` | 4.61 GiB | 1.25 GiB | Loaded, then stopped before generation |

These were sequential trials, not controlled paired performance measurements. Other applications and Windows memory reclamation changed the available RAM. All used the same open report and four CPU threads, a 4096-token context, one owned model at a time, existing package/hash checks and process memory limits. The isolated experimental startup and running free-memory floors were 1.5 GiB, with AC required, 8 GiB free disk, a 512 MiB additional paging budget and a ten-minute experiment deadline. Sampling is periodic, so the stop can occur below the floor. Production reserves remain unchanged.

The 1.5B model used 1.159 GiB peak sampled RSS and 1.319 GiB private memory. Startup, including attestation and loading, took 6.156 seconds. Four short requests took 1.22–1.59 seconds each. Mandatory hashing reads the weights first; these are not cold-SSD startup measurements.

The explicit mmap candidate took 11.438 seconds to load and reached 2.806 GiB sampled RSS / 4.103 GiB private memory before stopping. It recorded 31 MiB of additional system page output. This counter is system-wide; it does not prove which process caused paging. Earlier candidate runs recorded zero additional page output within their sampled intervals. All owned model workers exited; available RAM was 4.69 GiB at the final check.

## Why SSD and compressed cache did not solve this

The Q8 log confirms Flash Attention and a 68 MiB Q8 KV cache were actually active. Compressing this cache does not eliminate the multi-gigabyte model footprint. The explicit mmap log reports `compat patch disabled mmap for transformed text tensors` and `load_mode = none`, matching the previous investigation in `qwen-storage-and-memory-20260917.md`.

This installed multimodal package also loads a vision projector. Its log estimates up to 961.74 MiB for that component; this is an estimate, not measured reclaimable RAM. A separately qualified text-only model/package is a useful next experiment. Simply lowering the production memory threshold is not supported by these results.

## DAX correctness check

Four direct prompts asked for total sales, discount/sales using totals, sales for the Online channel and sales minus total cost. Each prompt specified the exact fields and relationship. The 1.5B model returned JSON but omitted required aggregations or produced invalid syntax. All four generated expressions failed native Power BI evaluation; all four reviewed reference expressions evaluated successfully against the same working report.

This is a small direct-prompt diagnostic with no retrieval, examples or repair pass. It does not measure the full production assistant pipeline or establish general model quality. No 4B response speed or correctness comparison is available because its resource checks stopped generation.

## Evidence and verification

- Harness: `scripts/benchmark_qwen_memory.py`; Ruff checks pass.
- Evidence: `.runtime/qwen-memory-20260921-run4/`, with per-variant `result.json`, `memory.json`, native logs and model metadata; `dax-validation.json` records native results and errors.
- `benchmark.py` in the evidence directory preserves the harness before formatting. The final variant added explicit mmap and additional failure/peak reporting; earlier variants used identical inference prompts and resource checks but lacked those extra reporting fields.
- Preparatory attempts in the earlier output directories stopped before loading because Power BI was closed or the experimental startup threshold had not yet been reduced. They are preserved separately.
- Studio still displayed the report's 46.76% and $73.51K cards after the experiments. No benchmark model worker remained running. The current production default was retained.
