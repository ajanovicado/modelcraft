> Historical local record. Referenced machine artifacts are excluded from this public source snapshot. Use the [README](../README.md) and [development guide](development.md) for current setup and tested scope.

# MiniCPM Windows runtime integration

The desktop Studio and workspace factories now read `config/defaults.yaml` and
select `MiniCPM5-2B-Q4_K_M` through an owned, loopback-only llama.cpp process.
The source runtime is the editable local package at
`C:\Users\developer\Desktop\PowerBI-RAG\runtime`. Keep that sibling project in place.
The old Ollama adapter remains available when an explicit Ollama profile is used.
There is no silent fallback to Qwen.

## Qualification status

The Mac's 14/14 artifact result is historical. It has **not** been reproduced on
this Windows PC. After initial startup guard failures, experiment 066 loaded the
model and saved 12/14 answers before the unchanged one-hour cap stopped it.
Review credited 7 artifacts for both language and semantics and 3 whole answers;
ROLE and STAR remain unfinished. Its unchanged scorer reports denominators over
the 12 saved answers, not the full 14-task suite. See the sibling PowerBI-RAG
`training/experiments/minicpm5-windows-066/REPORT.md` and `bounded-run-summary.json`.

Four predeclared fresh-data app checks passed **2/4**: SQL and model design passed;
DAX returned only a measure name, and M contained malformed table construction
and let bindings. Evidence is in
`.runtime/minicpm-fresh-7ad36028dc6b470687be60629cb5ae88/summary.json` and `review.json`.
These are narrow smoke checks in familiar families, not a general capability
benchmark. Manual review was by Codex, not an independent human.

The final packaged-engine health check verified the MiniCPM/llama.cpp identity,
but blocked startup at 7.90 GiB available RAM. The 8 GiB requirement remains in
force. The full build receipt is the sibling PowerBI-RAG
`WINDOWS-PORT-RECEIPT-20260916.md`, with binary hashes and results in its JSON
companion. The packaged health check itself did not run inference.

The publisher GGUF is pinned to repository revision
`2079a22f3beaa4e306449978533478fe0522f4b3`, SHA-256
`ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd`.
Its exact correspondence to BF16 revision
`3497c460c89e00520c3cfa2e73f49ab7647f1177` and the Mac MLX quantization is
unverified. It is an official frozen base GGUF, without adapters or training.

This PC reports 16 GB RAM and Intel Iris Xe graphics; it is not the 32 GB/NVIDIA
machine described in the older migration plan. CPU inference is configured.

## Safety and evidence

- Startup: AC, at least 8 GiB available RAM and disk.
- During inference: AC, at least 3 GiB available RAM and disk, no more than
  512 MiB new swap or native page output, one-hour process cap.
- The experiment keeps its 120-second generation/load cap. App generations use
  the configured 60-second budget, 4096-token context, 512 output tokens for
  structured requests and 384 for chat. Context overflow is rejected.
- A Windows Job Object enforces the 6 GiB model-process allocation ceiling and
  kills the owned server if its host exits. A watchdog also checks resident memory.
- The native Windows PDH `Memory\Pages Output/sec` cumulative raw counter is used because
  psutil's swap-out counter is zero on Windows. It conservatively includes file
  page output as well as paging-file output. Unknown telemetry fails closed.
- A cross-process lock prevents concurrent app/evaluation model loads. Model
  workers run hidden and unload at the end of each request.
- Every app request gets a unique evidence directory. Generation attempts,
  retrieved example IDs, partial failures and preflight failures are retained.
  No receipts are overwritten. Health checks do not load the model.

## What transfers to the app

The experiment port retains 063's task suites, family retrieval, validators,
scoring and bounded retry policy. It writes into new experiment directories;
the historical 063 answers and reports are untouched.

The app retains its own typed plans, schema validation, deterministic data
execution, relationship checks, DAX validation and approval gates. Its free-form
requests retrieve at most two pinned worked examples by lexical overlap. This is
a separate retrieval interface from the experiment's supplied family labels and
does not inherit the 14/14 result. Structured generation has at most one repair;
application semantic validators still decide whether a proposal is usable.

Native Power BI qualification remains unmeasured. Fresh-task quality has only
the four narrow checks above. Replay review found validator false positives for
M sorting, DAX syntax, invented relationship columns and a missing SQL alias;
the experiment's validators were preserved unchanged for this runtime port.
Do not use schema acceptance, a successful build, or the Mac report as evidence
that this Windows model produces correct artifacts.

## Reproduce

From `C:\Users\developer\Desktop\PowerBI-RAG`, run:

```powershell
$env:PYTHONUTF8 = '1'
.venv-windows\Scripts\python.exe training\experiments\minicpm5-cleanroom-063\test_experiment.py
.venv-windows\Scripts\python.exe -m pytest runtime\tests -q
.venv-windows\Scripts\python.exe training\experiments\minicpm5-windows-066\run.py evaluate
```

066 is already frozen. Do not rerun prepare, scoring or alter its inputs. The
measured run ended at its one-hour cap; it was not automatically restarted.
The runner supports resuming saved answers, but any future continuation must
be reported separately from this one-hour result. Any code/config change requires
a new experiment directory and manifest.
The source generator is `runtime/create_windows_experiment.py --name NAME`.

From the app repository, `uv sync --extra dev --extra desktop` installs the local
runtime package. Build the engine with `scripts/build-desktop-sidecar.ps1`, then
run `npm run tauri -- build --no-bundle` from `apps/desktop` to update the local
desktop executable. The old attested Streamlit/Qwen demo launcher remains a
historical workflow; use the Studio executable for the MiniCPM integration.

The updated executable is
`apps/desktop/src-tauri/target/release/ai-powerbi-studio.exe`; keep its adjacent
engine executable and both sibling project folders in place. The runtime also
uses the pinned llama.cpp binaries under the app's `.runtime/k2-llama-runtime`.
That existing local build is based on commit
`35999d101cf2233fc54f09c3c8d599da7303ce02` with a pre-existing Unicode tokenizer
patch; executable/DLL hashes are pinned in the runtime configuration. Both
thinking and nonthinking MiniCPM tokenization probes passed against the pinned
tokenizer before inference.

Previous app defaults and binaries are retained under
`.runtime/minicpm-migration-backup-20260916`. The original Mac shared runtime files
are retained under PowerBI-RAG's `archive/windows-port-20260916`.
