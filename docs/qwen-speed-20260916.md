# Qwen speed evaluation — 16 September 2026

The selected runtime remains **Qwen3.5:4b-q4_K_M, CPU, inference only**, with the original worked-example retrieval and unchanged validators. The new runtime reuses one guarded model session between requests and unloads after 120 idle seconds. Four paired fresh SQL tasks had median latency **24.797 s cold versus 14.891 s warm**, a **40.0% reduction**. Every SQL answer was identical between arms and executed correctly. Initial loading still costs roughly 10 seconds; this is a request-latency result, not a claim of 40% faster token generation.

The current configuration is `PowerBI-RAG/runtime/qualified-qwen-20260916-v5.json`. Source inference passed four fresh tasks, one separate regression case, and two streamed chat turns. The rebuilt desktop engine has the new runtime and verified shutdown behavior. **Packaged inference remains unverified:** its latest attempt correctly refused to start at about 7.7 GiB free RAM. It needs 8 GiB. Closing Codex before opening Studio should provide additional headroom, but that scenario has not been measured.

**Controlled latency evidence**

Four SQL prompts, K1–K4, were saved before any output. Both arms used the same model, original prompt ordering, original retrieval, deterministic settings and validators. The first pair ran cold then warm; the second pair ran warm then cold. Warm sessions were explicitly loaded before their timed requests. No other benchmark, test suite or build ran during this confirmation.

| Task | Cold request | Warm request | Result |
|---|---:|---:|---|
| K1, grouped hours | 25.203 s | 14.828 s | Identical SQL, correct execution |
| K2, sales revenue | 24.953 s | 15.719 s | Identical SQL, correct execution |
| K3, ticket counts | 24.641 s | 14.641 s | Identical SQL, correct execution |
| K4, weight filtering | 24.188 s | 14.953 s | Identical SQL, correct execution |
| Median | **24.797 s** | **14.891 s** | **40.0% lower latency** |

Warm startup took 9.594 s and 9.718 s. Including those two startups, the four requests took 79.453 s instead of 98.985 s, about 19.7% less overall time for these two-request sessions. One isolated request does not receive the full warm benefit.

The earlier eight-task P comparison showed 50.359 s versus 33.789 s median latency, with the same 5/8 correctness and identical delivered artifacts. That comparison also included changing desktop load and background development, so it is supporting evidence rather than an isolated measurement of the session change.

**Prompt policy and correctness**

The original lexical retriever still selects at most two examples from the unchanged cleanroom-063 corpus. An experiment put those examples before the current request. On the eight fresh H tasks, strict whole-answer correctness rose from 5/8 to 7/8. The remaining answer contained a false Power BI UI instruction despite correct core relationship advice. The original-order control was also warm; the comparison did not establish a general accuracy rate.

An additional four-task F check exposed an M regression: the all-domain prompt change generated invalid M twice, while the original-order control passed that task. Both arms omitted explicitly requested active status on the relationship task. Strict scores were therefore 2/4 for the candidate and 3/4 for the control. The all-domain change was rejected.

The final policy moves references before the current request **only for DAX and model relationships**, using the existing domain classifier. M, SQL and unclassified chat retain their original ordering. This does not reinstate the previously rejected retrieval replacement. Final source verification used four new V tasks, plus the previously failed M case R1:

| Case | Result | Wall time |
|---|---|---:|
| V1: SQL grouped freight costs | Correct DuckDB columns and rows: R=48, S=21 | 18.312 s |
| V2: DAX NetHeight | Correct measure and filter-context behavior | 11.985 s |
| V3: M calculated amount | Valid syntax; manually checked amounts 35 and 26 | 24.156 s |
| V4: Power BI relationship | Correct columns, 1:*, active Yes, single dimension-to-fact direction | 21.281 s |
| R1: prior M regression | Valid syntax; manually checked amounts 36 and 33 | 25.516 s |

That is **4/4 fresh tasks plus 1/1 regression case**, not five fresh tasks. Two subsequent source chat turns returned `Ready.`. SQL was executed in an isolated DuckDB connection. M was parsed with Microsoft's parser and manually reviewed. DAX and model semantics were manually reviewed with static checks; no native Power BI semantic execution was performed. These are small samples of known task families with new schemas and data, not proof of general correctness.

**GPU, Flash Attention and larger models**

The laptop has an i7-1165G7, four physical cores, 15.33 GiB usable RAM, Iris Xe graphics and NVMe storage. Official llama.cpp b11009 SYCL and Vulkan packages detected the GPU, but both rejected the installed GGUF's `qwen35.rope.dimension_sections` metadata: expected four entries, found three. No model bytes were changed.

Ollama's bundled Vulkan backend did offload all 34 layers when both experimental Vulkan and integrated-GPU enablement were set. GPU decoding was around 4–5 tokens/s versus roughly 6–7 on CPU. Flash Attention improved the GPU pilot, but the full eight-task GPU run hit the unchanged 3 GiB available-RAM floor on H08 after seven correct answers. A small background backup operation also occurred during that last task; this establishes failure under the observed desktop conditions, not universal GPU infeasibility. H08's copied prior-generation metrics are stale and excluded from throughput conclusions. CPU Flash Attention was slower in the three-task pilot. CPU with Flash Attention off remains the selected configuration.

No larger model or Colibri runtime was installed. Colibri's [Qwen documentation](https://github.com/JustVugg/colibri/blob/main/docs/qwen36.md) concerns a different model/runtime path; it is not a measured speed improvement for this installed dense Qwen model. Relevant upstream references are the [Ollama FAQ](https://docs.ollama.com/faq), [Ollama GPU documentation](https://docs.ollama.com/gpu), and [llama.cpp SYCL documentation](https://github.com/ggml-org/llama.cpp/blob/master/docs/backend/SYCL.md). A larger model was not justified within the retained memory limits.

**Resource protection and lifecycle**

The same Guard instance, paging baseline, one-hour clock, global exclusive lock, sleep inhibitor and watchdog remain active throughout a warm session, including idle time. Startup still requires 8 GiB available RAM/disk and AC power; running requires 3 GiB, with the existing 512 MiB paging limit and 6 GiB process protections. CPU threads remain four, context 4096, temperature zero, thinking disabled, output 512 tokens for structured requests and 384 for chat, 60 seconds per generation attempt, and at most one repair. The conservative input budget and STOP_REQUESTED behavior remain unchanged.

Each request receives a separate evidence directory. Concurrent requests are rejected. Idle expiry, cancellation and explicit unload stop owned workers. FastAPI shutdown unloads the model. A packaged smoke check additionally found that killing the PyInstaller launcher could leave its Python child alive. The new parent monitor requests server shutdown so the application lifespan runs; the final packaged test confirmed no remaining owned children. The final package did not load a model because RAM was insufficient, so real packaged shutdown with a loaded model is not yet established by that test. Source tests cover unload and cancellation with a live session.

**Validation and installed artifacts**

- Full suite before the launcher follow-up: **743 passed, 3 skipped, 5 subtests passed**. Skips require unavailable Windows symlink privilege.
- After the launcher follow-up and v5 configuration: **17 targeted tests passed**; Ruff passed for every changed production/test Python file.
- PyInstaller build succeeded. Both the bundled sidecar and portable release sidecar were updated. The GUI executable and installer were not rebuilt.
- Final engine SHA-256: `15e0b5c6d398d6c8bd72ad9816ef16f8b6162ec23420857614e991203efd57fc`.
- The original 947 historical files still match their preservation manifest. Original model-comparison reports, answers, receipts and corpus were retained. Configurations v2–v4 and candidate outputs remain historical evidence; v5 is current.
- With user authorization, OneDrive, Settings, the iCloud settings window, Windows Search and unused tool helpers were closed for headroom. Windows may restart background components. OneDrive can be reopened when syncing is needed.

**Evidence index**

Paths below are relative to `AI-PowerBI-Analyst/.runtime/`. Each completed grading run has a `quality-review.json`; detailed semantic decisions are in `qwen-speed-20260916/semantic-review-notes.json`.

| Evidence | Directory |
|---|---|
| Controlled K latency | `qwen-speed-confirm-2f69f6bbf0b64043b67aeab1a154d1c0` |
| P cold / warm | `qwen-speed-cold-1ff014e3a8ef445896c85cf66ae9b73f` / `qwen-speed-warm-37fe919eac574f7ea83696e739d54046` |
| H task-last / original | `qwen-speed-warm-0061da85fcdc43c2889e06230d578d1b` / `qwen-speed-warm-012f994496024e24a1c646938ea15382` |
| H GPU guard failure | `qwen-speed-warm-95cc1a5022e848f4810dbdc0c606726d` |
| F candidate / control | `qwen-speed-production-6ed4a6e4d1e647988913d91421fabb97` / `qwen-speed-production-control-f4f54812d35141b5bfb0a7852641c72a` |
| Final V and R source verification | `qwen-speed-final-eb459caaff7c4eb1a3ede81154d85cc8` |
| Initial packaged guard/cleanup failure | `qwen-speed-package-939635f778344c159acec7778c51134d` |
| Final packaged cleanup pass, inference guard block | `qwen-speed-package-debcec04169d4548a5db3ecd31b6b59d` |
| Tests, builds, archived sources and final receipt | `qwen-speed-20260916` |

The cold source files and original warm-only implementation are archived under `qwen-speed-20260916/before/`; the all-domain candidate is `warm_ollama-v3.py`. Historical manifests point to original source locations that now contain newer code: use the receipt's archived-source mappings when reproducing old runs. Scripts and frozen task payloads are in `evals/qwen_speed_20260916/`. All failed experiments remain available.
