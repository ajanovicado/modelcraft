# Text-only 4B integration — 21 September 2026

Studio now prefers the installed Qwen3.5 4B model through a managed text-only native runner. Qwen2.5-Coder 1.5B remains the automatic low-memory fallback. This follows the isolated experiment documented in `qwen-text-only-test-20260921.md`; the converted/mapped GGUF remains experimental and is not the active model.

## Runtime behavior

- Original pinned Qwen weights and installed, pinned llama-server executable; CPU only, four threads, one request slot, 4,096 context tokens, Q8 KV cache and 128 MiB prompt cache.
- No vision projector, offline loopback server, no web UI. The application still uses its retrieval and structured-output contracts.
- 4B startup requires 4.75 GiB free RAM; 1.5B requires 3 GiB. Both retain a 1.5 GiB running free-memory floor, AC requirement, additional paging cap, disk reserve, request deadlines and owned-process memory limits.
- 4B unloads after 60 seconds idle; 1.5B after 30 seconds. Selection is reconsidered after unloading. A partially streamed failed request is not silently replayed with another model.
- Standard profile rejects the former vision-loading backend, preventing that larger allocation under the new memory budget.
- Native completion explicitly closes the reasoning prefix. Without this, a free-form chat test produced thinking output and timed out; the corrected chat answered normally.
- The native runner checks its actual tokenizer output plus the reserved answer tokens against 4,096. The former byte-count upper bound rejected an ordinary browser DAX conversation even though the native prompt had only 941 tokens. Oversized prompts still fail explicitly; they are not silently truncated. The coder backend retains its existing conservative bound.

## Verification

- 74 targeted text-only, adaptive-selection and DAX tests passed; 43 existing warm-runtime, managed-runtime, guarded-runtime, contract and hardware-profile tests passed. After adding native token admission, 54 focused tests passed, including the new boundary test. Ruff passed on all changed Python sources/tests.
- Actual production service path: normal filter-context chat and schema-grounded total-sales DAX generation passed. The DAX assistant validated its candidate using the native Power BI engine before returning it. Evidence: `.runtime/text-only-integration-20260921/live-check-r2.json`.
- Restarted backend and refreshed browser diagnostics: `qwen3.5:4b-q4_K_M`, ready, text-only profile, 4.75 GiB startup reserve.
- Browser DAX conversation generated `SUM(FactSales[SalesAmount])`, populated the editor and automatically previewed 729,782,919.215398 through Power BI Desktop. The actual native worker was sampled at 2.84 GiB resident with Desktop and Studio open.
- A browser follow-up, "Now subtract the sum of FactSales TotalCost from it", generated `SUM(FactSales[SalesAmount]) - SUM(FactSales[TotalCost])` and automatically previewed 414,617,394.985394, matching the native reference value. Both requests used the new 4B runner.

These are focused integration checks, not proof of general DAX correctness. The earlier raw-prompt diagnostic contained wrong formulas, and generated calculations still need execution validation. The smaller fallback has less capacity. SSD mapping is not enabled by this integration: the tested mapped variant reduced private memory but retained a similar resident working set and processed prompts more slowly.
