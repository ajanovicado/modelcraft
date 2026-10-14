> Historical local record. Referenced machine artifacts are excluded from this public source snapshot. Use the [README](../../README.md) and [development guide](../development.md) for current setup and tested scope.

# K2 Horizon installation: blocked before qualification

**Latest status (September 6): runtime now loads and generates after the user disabled Smart App Control and a local tokenizer fix was built. Chat/JSON smoke tests passed; LOD correctness and throughput gates failed. Not integrated or selected in the app. See the final section.**

Date: 2026-09-05. Requested outcome: install K2 alongside Qwen and make K2 the default.

## Completed

- Downloaded the approved community Q4_K_M quantization from https://huggingface.co/0xzknw/K2-Horizon-7B-GGUF at revision `d8f7593cdaf25fc4b75b7ab297bf467b94c42f34`.
- Verified all 5,592,218,272 bytes against SHA-256 `66371e17b1ee10b8ae36c6c1c9c8ebbb29ae80d006f8951e15dfc4bea082fb4a`.
- Model retained at `C:\Users\developer\AppData\Local\AI Power BI Analyst\models\k2-horizon-7b-q4\K2-Horizon-7B-Q4_K_M.gguf` (Codex packaged-app filesystem virtualization applies).
- Built the publisher's compatible runtime from https://github.com/MBZUAI-IFM/llama.cpp, branch `model/K2Horizon`, commit `35999d101cf2233fc54f09c3c8d599da7303ce02`, using Visual Studio 2022 Build Tools, Release x64, CPU AVX2, no OpenMP or embedded web UI.
- Retained runtime binaries beside the model under `runtime`; source/build retained under `.runtime/k2-llama-runtime`.

Runtime executable SHA-256: `1aa7c52bbd6b46c798aae28845324715589896af23c8dd0e5fa64b39d5281e7e`.

`ggml-base.dll` SHA-256: `968a66bebfaa1548875e903ab3bc2aa82f0110482cdd5d7c5482d79fba921949`.

## Confirmed blocker

The first `llama-server.exe --version` invocation exited with code 1 and no output. Windows Code Integrity events 3033 and 3077 at 17:01:26 local time identify the runtime's `ggml-base.dll` as failing signing requirements. Event 3118 identifies a Smart App Control block. Authenticode inspection reports `NotSigned`.

The policy ID in event 3077 is `{0283ac0f-fff1-49ae-ada1-8a933130cad6}`. The event resolves the runtime under the packaged-app path `C:\Users\developer\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\AI Power BI Analyst\models\k2-horizon-7b-q4\runtime`.

No alternate execution method, security-policy modification, or protection bypass was attempted after this diagnosis.

## Application state and remaining work

Qwen remains the default. No K2 inference, structured-output, throughput, memory, UI selection, or dashboard qualification test has passed. The experimental adapter was removed rather than leaving an unverified runtime integrated into production. No user dashboard or PBIP was changed for this installation.

Continuation requires a compatible runtime trusted by this machine's existing security policy, or an administrator-approved deployment through the normal policy process. Do not disable Smart App Control as an installation step.

After that prerequisite: test loopback-only inference and strict schema generation; implement persistent model selection and per-response model attribution; verify cancellation, memory release, and Power BI launch; qualify dashboard planning; then switch the default to K2. Keep Qwen available for comparison and fallback. Do not label an installed model as ready merely because its files exist.

## September 6 recheck and supported resolution

- Read-only `Get-MpComputerStatus` confirms `SmartAppControlState: On`.
- Authenticode inspection confirms all eight retained runtime EXE/DLL files are unsigned, not only `ggml-base.dll`. Signing the launcher alone would not resolve the dependency checks.
- Re-read the September 5 Code Integrity event 3077: the blocked dependency and policy match the original diagnosis. The blocked executable was not retried.
- Ollama reports version `0.33.3`. No working K2 runtime or K2 inference was established.
- The [official model card](https://huggingface.co/IFM/K2-Horizon-7B-GGUF) still specifies K2-aware llama.cpp and directs users to the publisher's development fork. The upstream architecture registry inspected on September 6 contains no `horizon` entry.
- The [publisher's releases page](https://github.com/MBZUAI-IFM/llama.cpp/releases) lists no releases. No publisher-provided signed Windows package was found.
- Microsoft's [signing guidance](https://learn.microsoft.com/en-us/windows/apps/develop/smart-app-control/code-signing-for-smart-app-control) requires certificates from trusted providers for Smart App Control trust. Its [FAQ](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/smart-app-control-frequently-asked-questions) says individual-app exceptions are not available.

Supported next step: obtain a K2-compatible Windows runtime with a trusted signature chain, including its executable dependencies, from the publisher or through an authorized code-signing/deployment process. This requires publisher/administrator coordination or access to an appropriate signing service; none was authorized or provisioned during this investigation. Do not create a self-signed trust exception, weaken Windows policy, or use alternate loaders/environments to evade the confirmed block.

Once a trusted build is supplied: verify provenance and all signatures, pass a version smoke test, load the existing verified Q4 model on a loopback-only authenticated endpoint with a bounded context, check memory and cancellation, then qualify structured planning and the numerical LOD example before app integration or changing the default. Re-downloading the model and changing PowerShell execution policy do not address the diagnosed architecture/signature blockers.

## September 6 user-initiated setting change and successful runtime retry

The user reported turning Smart App Control off and explicitly requested a retry. A read-only status check confirmed `Off`; the agent did not change any Windows security setting. The original `llama-server.exe --version` then passed (MSVC 19.44.35228.0, commit 35999d1).

Model load exposed a separate Windows regex error in the K2 vocabulary pattern: MSVC could not handle the `\\u200C` / `\\u200D` escapes in the collapsed regex. Patched `src/unicode.cpp` to use the existing code-point splitter with K2's combining-mark and joiner extensions, retaining three-digit grouping. Added an empty-input fast path. The original model bytes and vocabulary metadata were not changed. Reproducible patch: `scripts/k2-windows-tokenizer.patch`; local regression harness: `.runtime/k2-tokenizer-test` (13 cases passed, covering empty input, contractions, whitespace, numbers, combining marks, both joiners, and DAX punctuation). These focused tests are not full tokenizer parity qualification.

Rebuilt executable: `.runtime/k2-llama-runtime/build-studio/bin/Release/llama-server.exe`. Final `llama.dll` SHA-256: `750697cf4c6528cf41bedf383252e788f5882e6fbb7482959c106882c84e251a`. The original runtime beside the model remains unchanged. The final rebuild includes the additional empty-input fix; chat/JSON smoke evidence below was captured on the immediately preceding tokenizer-patched build.

The initial 2,048-context load with default weight repacking exhausted comfortable memory headroom and was stopped. Successful low-memory test configuration:

- loopback `127.0.0.1:8091`, per-launch random bearer token via `LLAMA_API_KEY` (not recorded here);
- context 1,024; one slot; four generation and batch threads;
- batch 64 / microbatch 32; CPU-only; `--no-repack`;
- Q8 K/V caches and Flash Attention on;
- request `chat_template_kwargs.enable_thinking=false` (server reasoning budget alone did not suppress thinking).

Observed loaded working set approximately 5.19 GB and available physical memory approximately 3.5–3.7 GiB. Requests without authentication returned HTTP 401; listener bound only to loopback.

Smoke results:

1. `What is 2 + 2? Reply with the number only.` -> `4`, completed normally.
2. Schema-constrained object with integer `answer` -> `{"answer": 4}`, completed normally.
3. Region-fixed DAX test -> **FAIL**. Actual generated expression:

```dax
Regional Sales LOD =
VAR BaseSales = SUMX('Orders', 'Orders'[Sales])
RETURN
    CALCULATE(BaseSales, REMOVEFILTERS('Orders', 'Orders'[Region]))
```

This does not preserve Region as requested. It also captures a scalar variable before CALCULATE and supplies an invalid table-plus-column REMOVEFILTERS argument combination. No DAX or dashboard changes were applied.

Generation measured about 4.05–4.14 tokens/second (49-token DAX sample: 4.124 tokens/second; prompt processing 12.84 tokens/second). This is below the earlier 6-token/second gate, and one short JSON sample does not qualify structured planning. Full context, streaming cancellation, model switching, and end-to-end Power BI build qualification remain untested. The probe server was stopped after tests to free memory; Qwen remains the app default. No K2 production integration or readiness claim is warranted yet.
