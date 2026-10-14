# Text-only Qwen 3.5 and converted-GGUF test — 21 September 2026

Follow-up: the original-weight text-only runner was subsequently integrated at the user's request. See [the integration record](qwen-text-only-integration-20260921.md) for current settings and production-path checks. The experiment below records the state before that integration.

**The 4B model can run alongside Power BI when vision loading is removed.** A separately converted model also ran with confirmed memory mapping after CPU weight repacking was disabled. This revises the earlier finding: the installed multimodal loading path failed the memory test; the text-only paths completed this short pilot. Neither candidate has been enabled in production.

## Is Studio using vision?

The current chat, artifact generation and DAX model paths send text messages and structured-output schemas. Inspection found no image payload fields in these paths. The app captures report images for previews, but those captures are not sent to the LLM. Ollama nevertheless passes the original monolithic Qwen GGUF as a vision projector and loads it at startup.

## Experiment

Both successful variants used the installed, hash-verified llama-server binary directly, with the current Qwen 3.5 4B weights, no projector, CPU only, four threads, 4096 context, Q8 KV cache, Flash Attention, greedy generation and 160 output tokens. We used four existing short DAX diagnostic prompts, without retrieval, few-shot examples or a repair pass. Native completion used the simple text-only, thinking-disabled Qwen35 ChatML renderer branch. This is an isolated native-runtime experiment, not an end-to-end production Ollama integration test.

Power BI Desktop and Studio remained open. Resource checks retained a 1.5 GiB free-memory floor, 512 MiB additional system paging budget, AC requirement, 8 GiB disk reserve, six-GiB owned-process memory cap, bounded load/request deadlines, a ten-minute total deadline and an exclusive runtime lock. These are experimental resource thresholds; production settings were not edited.

| Measurement | Original weights, text only | Converted weights, mmap, no repacking |
| --- | ---: | ---: |
| Completed requests | 4/4 | 4/4 |
| Peak sampled resident memory | 2.750 GiB | 2.751 GiB |
| Peak sampled private memory | 3.195 GiB | 0.755 GiB |
| Lowest sampled system free RAM | 2.079 GiB | 2.245 GiB |
| Model load time, after attestation | 4.187 s | 2.078 s |
| Median request time | 5.468 s | 7.891 s |
| Decode rate | 6.64–7.06 tokens/s | 6.82–7.02 tokens/s |
| Additional system page output | 28.32 MiB | 1.62 MiB |
| Correct native DAX results | 2/4 | 1/4 |

The mmap configuration still had about 2.75 GiB resident. Its 0.755 GiB private-memory figure is **not** its total RAM requirement: mapped model pages also consume physical memory while resident. The large reduction in private memory makes more of its weight storage file-backed and reclaimable; this experiment did not demonstrate useful inference with the active weights largely absent from RAM. Page-output counts are system-wide, not model-specific. Hash verification reads the model before loading, so these are not cold-SSD measurements. These were sequential single trials with varying background load; timings and quality counts should not be treated as a broad benchmark.

## Conversion and the failed intermediate trial

The installed `llama-quantize.exe` supports COPY mode. We used it with a 64 MiB tensor buffer limit to write a separate GGUF, applying the installed compatibility transformations without lowering weight precision or overwriting the original model. The converted file is 2,722,262,272 bytes, SHA-256 `2df892a9b08eaaf64d4f1d7e9eb04d31d54439e80c39cae0dfad79f6f2d8cc49`. Its text tensors were written in the loader's expected layout. The conversion finished in about 5.5 seconds of native tool time; its sampled peak RSS was 0.111 GiB. See the [installed version's compatibility documentation](https://github.com/ollama/ollama/blob/v0.33.3/llama/compat/README.md) for how tensor transformations are applied by the quantization tool.

The first converted-model trial confirmed `load_mode = mmap` but retained CPU repacking. The log showed 2,513.56 MiB mapped weights plus a 1,700.86 MiB CPU_REPACK buffer. It hit the free-memory floor before generation and was stopped. The lowest sampled free RAM was 0.393 GiB; resource sampling is periodic, so a fast allocation can cross below the floor before termination. The next trial used the installed runtime's supported `--no-repack` flag and completed successfully, with no CPU_REPACK buffer. This avoided that extra allocation at the cost of slower prompt processing in this pilot.

## Quality verification

Generated expressions were evaluated read-only through Studio's native Power BI DAX engine against the open working report. Reference values came from the same native engine and dataset in the earlier diagnostic:

- Total sales: 729,782,919.2153981.
- Discount amount / sales: 0.016520464619207323.
- Online channel sales: 154,974,383.54380003.
- Sales minus total cost: 414,617,394.98539406.

Both variants computed total sales correctly, both generated malformed discount-rate DAX, and both omitted the Online filter. Original text-only weights produced correct profit DAX; the mmap/no-repack variant produced invalid reference syntax. The variants change both converted representation and CPU kernels, so this single differing answer cannot be attributed to conversion alone. Do not promote either based on this small quality test. Validate the production retrieval, structured contracts, repair flow and fresh multi-turn holdouts before activation.

## Evidence and state

- Harness: `scripts/benchmark_qwen_text_only.py`; Ruff checks pass.
- Original text-only results, conversion log/artifact and failed repacking trial: `.runtime/qwen-text-only-20260921/`.
- Successful mapped trial: `.runtime/qwen-text-only-20260921-no-repack/converted-text/`.
- Native DAX results: `.runtime/qwen-text-only-20260921/dax-validation.json`.
- Each trial records command, original/converted model hash, raw responses, memory samples and native logs.
- All benchmark model workers exited. No model download, original-weight edit, production-profile edit or Windows pagefile change was performed.

The useful next step is production-path quality qualification of text-only 4B, with the mapped/no-repack configuration as a lower-private-memory candidate. The experiments establish feasibility, not complete application integration or reliable DAX generation.
