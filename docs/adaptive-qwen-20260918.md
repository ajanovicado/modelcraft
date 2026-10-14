# Adaptive local Qwen — 2026-09-18 UTC

The app now selects an installed, pinned Qwen profile from both usable physical
RAM and current free RAM. This is inference only. The historical experiments and
all failed trials remain preserved. No retired model was restored.

| Automatic profile | Model | Startup free RAM | Job/process cap | Idle unload |
| --- | --- | ---: | ---: | ---: |
| Standard | Qwen3.5 4B Q4_K_M | 7.25 GiB | 6 GiB | 120 s |
| Light | Qwen2.5-Coder 1.5B Instruct Q4_K_M | 4.5 GiB | 2.5 GiB | 30 s |

Standard requires at least 12 GiB usable installed RAM; Light requires 6 GiB.
An 8 GB laptop can therefore select Light if sufficient RAM is free. A busy
16/32 GB laptop can also select Light. Total installed RAM alone cannot override
the free-memory requirement. No cloud fallback or training is used.

The only relaxed resource threshold is the original fixed 8 GiB **startup RAM**
requirement, replaced by model-specific budgets. The original guard implementation
is unchanged. The 3 GiB running RAM/disk floor, 8 GiB startup disk floor, AC power,
512 MiB additional paging cap, one-hour session, stop file, exclusive process
lock, job termination, deadlines and one bounded repair remain enforced. Model
selection never bypasses a guard. There is no promise to run with exhausted RAM.

Light's 4.5 GiB budget leaves approximately 0.28 GiB beyond the measured
1.22 GiB peak resident model memory and 3 GiB running reserve. The running guard
still stops inference if other applications consume that headroom. Standard's
7.25 GiB startup budget is based on the earlier 3.83 GiB observed peak plus the
same reserve and margin; a fresh Standard run in this session was blocked by RAM.

## Measured confirmation

Host: HP EliteBook 840 G8, i7-1165G7, four physical CPU cores, 16 GB installed
(15.33 GiB usable), Windows, AC power, CPU inference through Ollama 0.33.3.
The official [Qwen Coder package](https://ollama.com/library/qwen2.5-coder:1.5b-instruct-q4_K_M)
is pinned to digest `d7372fd828518a4d38b1eb196c673c31a85f2ed302b3d1e406c4c2d1b64a0668`.

Eight new D-series tasks covered SQL filtering/grouping, DAX SUMX/DIVIDE,
Power Query filtering/sorting/added columns, and one-to-many relationships.
Expected results were fixed before generation. All eight passed on their first
attempt. SQL was executed against fixtures. DAX, M and relationships received
parser/contract checks plus manual source review, **not native Power BI execution**.

| Metric | Light confirmation |
| --- | ---: |
| Correct artifacts | 8/8 |
| Median request time, including cold first request | 6.27 s |
| First request | 7.80 s |
| Peak model process-tree resident RAM | 1.218 GiB |
| Peak private/committed process-tree memory | 1.329 GiB |
| Free RAM before starting, after controlled reservation | 4.68 GiB |
| Minimum free RAM sampled after tasks | 3.45 GiB |
| Additional page-outs | 0 bytes |
| Owned workers left after shutdown | 0 |

The test process reserved 1.61 GiB of its own physical memory to reproduce lower
free RAM, then released it. This is **not physical 8 GB hardware qualification**.
The tasks are fresh instances of eight bounded task templates; the result does
not establish accuracy on arbitrary complex DAX, M, or multi-table plans.
There is no new fair 4B-versus-Light speed claim: their available results used
different tasks and conditions. The earlier 4B result remains 7/8 and 20.3 s median.

## Changes that mattered

- Small-model SQL/M retrieval uses a pinned language-specific example library;
  filter wording expands retrieval terms. The actual current request follows the
  examples. Standard retains its prior retrieval and ordering.
- Light relationship generation constrains endpoint spelling to supplied schema
  references; semantic checks still enforce cardinality, active status and direction.
- DIVIDE checks retain an explicitly requested alternate result, including zero.
- Complete optional history turns/examples are removed before a prompt overflows;
  current user/schema text is never silently truncated. Receipts distinguish
  retrieved references from references actually included. Repairs remain bounded.
- Workspace chat uses a smaller, whole-record dataset profile and reference budget.
- Saved-prompt caches are 128 MiB for Standard and zero for Light. Recurrent-state
  checkpoints are capped at two, preventing accumulation across requests.
- Settings reports the actual selected model and memory budget. Health refreshes
  after a chat. Idle unload/cancellation release the owned model process tree.

## Rejected trials

| Trial | Correct / attempted | Median request |
| --- | ---: | ---: |
| Qwen3.5 2B initial pilot | 5/8 | 15.60 s |
| Qwen3.5 2B reordered references | 4/8 | 15.95 s |
| Qwen3.5 2B scoped references | 3/8 | 15.13 s |
| Qwen3.5 0.8B confirmation | 3/8 | 11.05 s |
| Qwen Coder initial policy | 3/8 | 8.87 s |
| Qwen Coder revised policy, lower free RAM | 8/8 | 6.27 s |

These sequential development trials are not controlled model comparisons.
Qwen3.5 2B and 0.8B are excluded from automatic selection. Their models and
create-once receipts remain available for reproducing the failed experiments.

## Evidence and configuration

- Active catalog: `../PowerBI-RAG/runtime/adaptive-qwen-20260918-v2.json`.
- Source/config archives, downloads, failed and successful receipts:
  `.runtime/adaptive-run-20260917-213235/` and `.runtime/adaptive-qwen-20260917-190116/`.
- Executed SQL results and explicit review decisions: `analysis.json` in the first directory.
- Controlled-memory test: `coder-capacity/capacity.json`, `released.json`, and
  `evaluation/{manifest,results}.json`; each answer has its own immutable receipt.
- Reproducible runner and fresh tasks: `evals/adaptive_qwen_20260917/`.
- The active catalog enables Standard and Light only. Candidate configs are not
  deployment approvals. All previous runtime versions remain untouched.

## App verification

The live flow was `http://127.0.0.1:5173/` → Settings → Workspace → send a
discussion question → completed reply → ask model identity. The Codex in-app
browser showed the Light profile, its 4.5 GiB startup budget and Ready status.
The fresh discussion request completed in 7.36 seconds of model processing,
with 563 prompt tokens and 49 generated tokens. Identity correctly reported
Qwen Coder 1.5B without inference. No draft/build action occurred.

| Check | Result |
| --- | --- |
| Page identity and nonblank content | Pass; AI Power BI Studio, localhost |
| Framework overlay | None |
| Browser error/warning logs | None |
| Settings and chat interaction | Pass; screenshots emitted in task |
| Desktop viewport | 2330 × 1272 |
| Mobile/physical 8 GB device | Not tested |
| Python verification | 129 targeted cases covered; four identity regressions fixed, affected 27-case suite rerun green |
| Frontend chat/designer tests | 31 passed |
| TypeScript and Vite production build | Passed; existing large Vega bundle warning remains |
| Ruff on changed runtime/service/tests/scripts | Passed |
| Active profile pins | 67 Standard and 68 Light pins verified |
| Historical pinned files changed | All nine have verified preserved copies |
| Model workers after idle unload | None; localhost app servers remain running |

The discussion smoke test establishes delivery and resource handling, not
comprehensive answer accuracy. Its explanation was generic and the documentation
retriever supplied an unrelated YEARFRAC reference. General documentation
retrieval relevance remains a separate quality limitation; the 8/8 artifact
result must not be generalized to all chat questions or complex report builds.
