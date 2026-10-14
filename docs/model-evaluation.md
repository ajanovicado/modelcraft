# Local Model Selection

## Shipping rule

The model remains configurable. The exact shipping tag and digest are frozen
only after the 64-case corpus runs three times on the target Windows laptop.
The winner must pass correctness, memory, latency, and offline gates; reputation
or public benchmark position is not sufficient.

## Verified Ollama candidates

The official Ollama library currently exposes:

- [`qwen3.5:4b-q4_K_M`](https://ollama.com/library/qwen3.5/tags), with tool support and
  an official package size of about 3.4 GB.
- [`phi4-mini:3.8b-q4_K_M`](https://ollama.com/library/phi4-mini/tags), with tool
  support and an official package size of about 2.5 GB.

The pinned primary is `qwen3.5:4b-q4_K_M`.
Its published size sits slightly above the project's 3.3 GB resident-memory
target, making the target-laptop measurement a real pass/fail gate. Phi-4 Mini
is retained as the lower-memory fallback.

Gemma 4 E4B was screened from the 8 GB target-laptop run: the official Ollama
tag is currently about 9.6 GB, well beyond the allowed model budget. This is a
documented resource disqualification, not a quality ranking. See the official
[`gemma4` tag list](https://ollama.com/library/gemma4/tags).

## Fixed evaluation conditions

- Exact `config/defaults.yaml` prompt and context.
- Temperature 0, 4,096-token context, 512-token configured output ceiling;
  the visual-page blueprint call is further bounded to 160 output tokens.
- Three runs per case and candidate.
- Cold and warm latency recorded separately.
- Ollama tag, digest, version, CPU, RAM, and Windows build recorded.
- Networking blocked except loopback.
- Model resident memory and minimum available system memory sampled during the
  run.

## Acceptance

- Route macro-F1 ≥0.95.
- Schema-valid plans after one retry ≥98%.
- Active-catalog object IDs valid 100%.
- Exact numerical golden answers for all 32 applicable cases. The model-run
  recorder must compare each measured plan and `EvidenceAnswer` with
  `evidence/golden-answers.jsonl` through the repo-owned evaluator and derive
  the rate from the 32 per-case outcomes; hashes alone are not correctness.
- Unsafe writes and critical hallucinations: zero.
- Warm generation ≥8 tokens/second preferred; below 6 triggers a fallback.
- Resident model memory ≤3.3 GB on the 8 GB target.
- At least 750 MB available without sustained paging.

The deterministic form/rule path is always available for Build. A model that
fails the plan gate may still be used for grounded plain-language explanation
only if it passes grounding and latency tests.
