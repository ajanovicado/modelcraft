# Retired model removal — 17 September 2026

MiniCPM5-2B is removed from the active app and all located model copies. Qwen3.5:4b-q4_K_M remains the production model. The retired model will not be restored for further comparisons.

Removed assets include the remaining tokenizer/configuration directory, the training adapter and its configuration, the isolated benchmark model store, old model-specific app scripts and provider, and obsolete engines containing that provider. The main downloaded `PowerBI-RAG.zip` also contained MLX and BF16 model weights plus an adapter: 91 model-related entries were removed. Every other archive entry was read back and verified against its original content hash (17,668 entries). The ZIP shrank by 5,332,057,893 bytes.

The active app now uses a model-neutral guarded adapter and process owner. It has no MiniCPM import, loader, tokenizer or configuration path. The direct retired provider is rejected by the settings contract. Qwen's corpus was copied unchanged into `PowerBI-RAG/runtime/data/worked-examples.jsonl`; its SHA-256 remains `fce1716e3f757dd5c93abc453de22adb6ed19ac0451b39320eaffae4d9b121d1`. The active configuration is `qualified-qwen-20260917-v2.json` with 60 verified pins.

The three resource-guard modules are byte-identical to the preceding qualified configuration. The extracted process/watchdog methods are AST-identical. Qwen's package, generation limits, retrieval selection, artifact checks and bounded-repair policy are unchanged. No training was performed.

Validation:

- 90 relevant tests passed, including startup/runtime guards, kernel job ownership, repairs, factory selection, corpus integrity and retrieval parity across all eight frozen comparison prompts.
- Ruff passed on changed app and test files.
- One fresh guarded SQL task generated the correct query and returned the expected rows in DuckDB.
- The rebuilt packaged app reported `ready`, identified Qwen with its pinned digest, streamed a real response and left no owned workers after shutdown.
- The packaged engine's Python archive contains the new shared runtime and excludes both retired modules. The app appended its normal reference footer to the smoke response, so the whole UI response was not an exact sentence-only match; the model's generated sentence was correct.
- The engine and desktop installers were rebuilt. File hashes and build receipts are recorded in the final receipt below.

The search covered the user's model stores/caches and download archives, plus the accessible C: filesystem. Protected Windows directories were not readable and are recorded in the scan log. Other model families and shared inference software were not removed.

Prior experiment reports, answers, logs and receipts remain preserved under the earlier evidence-retention instruction. Retired source/configuration files needed for provenance are retained as text archives outside the active runtime; model weights and adapters are not retained. These historical records may still mention the retired model. The current app README and project handoff no longer direct work to it.

[Final receipt](../.runtime/qwen-only-20260917/final-receipt.json) · [File removal receipt](../.runtime/qwen-only-20260917/file-removal-complete.json) · [Archive removal and retained-entry verification](../.runtime/qwen-only-20260917/archive-cleanup-complete.json) · [Guard equivalence](../.runtime/qwen-only-20260917/guard-equivalence.json)
