# Target Windows Setup

This is the production-like client-demo environment. Do not enable real Power
BI writes until every Phase 0 check is recorded in `evidence/gates`.

## Required host

- Windows 11 on the target i5/i7 laptop.
- 8–16 GB RAM, CPU inference, no GPU dependency.
- Power BI Desktop with PBIP/PBIR support.
- Python 3.12.12 and `uv`.
- Node.js LTS.
- Ollama.

Record the exact Windows build, CPU, physical RAM, Python, Node, Ollama, Desktop,
CLI, validator, and model digest before testing.

## Pinned report tools

Install pbi-cli in an isolated Python environment at the project pin:

```powershell
uv tool install --prerelease=allow "pbi-cli-tool==3.11.1"
```

Install the independent Microsoft PBIR validator at the project pin:

```powershell
npm install --global "@microsoft/powerbi-report-authoring-cli@0.1.4"
npm install --global "@microsoft/powerbi-desktop-bridge-cli@0.1.2"
```

Confirm the actual executables and freeze their help output:

```powershell
pbi --version
pbi --help
powerbi-report-author --version
powerbi-report-author --help
powerbi-desktop --version
```

The command output—not this document—is the authority. Any drift blocks real
mode until the adapter contracts and golden tests are updated deliberately.

## Project setup and online provisioning

The repeatable online setup entrypoint installs the pinned Python/Node tools,
pulls the exact Qwen quantization, records its full digest, and runs preflight:

```powershell
.\scripts\provision-local-ai.ps1
```

Qwen 4B is the only model the app uses; there is no fallback model to pull.

```powershell
Set-Location "$HOME\Desktop\AI-PowerBI-Analyst"
uv sync --python 3.12.12 --extra dev
uv run --extra dev ai-powerbi-analyst preflight
```

Pull the one pinned model:

```powershell
ollama pull qwen3.5:4b-q4_K_M
```

After provisioning, disconnect or block non-loopback networking for the
offline checks. The launcher itself never installs, downloads, or pulls.

## Prepare the governed workspace

Power BI Desktop must be closed. Run preparation once:

```powershell
.\scripts\prepare-windows-demo.ps1
```

This creates `fixtures\demo\workspace`, an immutable reset baseline, and an
unattested `project-bundle.json`. It hydrates every TMDL partition from the
frozen Parquet sources and records report, semantic-model, DuckDB, catalog, and
source-data hashes. It refuses to overwrite an existing demo workspace.

The expected fail-closed check is:

```powershell
python -m ai_powerbi_analyst.runtime.preparation verify `
  --repository-root . `
  --demo-root .\fixtures\demo
```

Adding `--require-attested` must remain blocked until the Phase 0 evidence has
actually passed.

## Phase 0 kill-test order

1. Close Power BI Desktop and verify the process is absent.
2. Open the prepared PBIP once, refresh every imported table from the frozen
   local Parquet sources, save, and close Desktop. Confirm that
   `ClientWow.SemanticModel\.pbi\cache.abf` is nonempty. Seal only that cache
   and Desktop local settings into the reset baseline:

   ```powershell
   .\scripts\capture-phase0-windows.ps1 -SealCache
   ```

   Cache sealing rejects any concurrent TMDL or semantic-model metadata drift,
   freezes the cache into both the live workspace and recoverable reset
   baseline, and recomputes the bound semantic-model/baseline hashes. It does
   not assert that Desktop rendered populated visuals; the cold-render command
   later captures that independent proof.
3. Capture the canonical validator pair once. Both must pass before either
   canonical named receipt is published; a failed attempt remains only as
   immutable content-addressed diagnostic evidence.

   ```powershell
   .\scripts\capture-phase0-windows.ps1 -CaptureValidators
   ```

   Later `-RunKillTest` and `-CaptureColdRender` invocations reuse this exact
   pair instead of recapturing it with different timing bytes.
4. Capture the actual Windows, Desktop, tool-executable, version, and installed
   Ollama model identities. Replace the example values with values read from
   the target laptop; the recorder independently probes them and rejects a
   caller pin or executable mismatch.

   ```powershell
   .\scripts\capture-phase0-windows.ps1 `
     -CaptureEnvironment `
     -DesktopExecutable "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe" `
     -DesktopVersion "2.146.705.0" `
     -WindowsBuild "10.0.26100" `
     -ModelTag "qwen3.5:4b-q4_K_M" `
     -ModelDigest "sha256:<64 lowercase hex characters>"
   ```
5. Capture `pbi --help` plus help for every report-layer group used by the
   adapter.
6. Capture one success and one failure JSON envelope for every wrapped command.
7. Verify report-layer reads and writes with no Desktop connection.
8. Validate the clean baseline with pbi-cli, local schema/binding checks, and
   `powerbi-report-author` while non-loopback networking is blocked.
9. Repeat representative create/modify operations ten times from the clean
   placeholder baseline. Each iteration receipt must bind its exact
   `ReportChangePlan` recipe hash, derived output hash, one pbi-cli receipt, one
   Microsoft-validator receipt, a Desktop executable/screenshot receipt, and
   an exact rollback hash.

   ```powershell
   .\scripts\capture-phase0-windows.ps1 `
     -RunKillTest `
     -DesktopExecutable "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe" `
     -DesktopVersion "2.146.705.0"
   ```
10. Open every derived output in the pinned Desktop build and inspect all pages.
   The three-page render receipt must bind a derived mutation output—not the
   one-page placeholder baseline hash.
11. Inject a failed binding and a killed worker; verify byte-equivalent rollback.
12. Run the repository-owned exact 64-case corpus three times for each
    candidate. The recorder persists all 192 raw outcomes, exact expected and
    actual plan/answer hashes, mismatch paths, and independently derives the
    numerical family from the 32 frozen goldens.

    ```powershell
    .\scripts\capture-phase0-windows.ps1 `
      -CaptureModelEvaluation `
      -ModelTag "qwen3.5:4b-q4_K_M" `
      -ModelDigest "sha256:<64 lowercase hex characters>"
    ```
13. Disable every physical non-loopback adapter, then run the repository-owned
    complete-flow and schema checks between two independent adapter probes.

    ```powershell
    .\scripts\capture-phase0-windows.ps1 -CaptureStrictOffline
    ```

    An arbitrary helper executable or a hand-authored success document is not
    accepted as offline proof.
14. Perform a clean reset, rebuild from the reset baseline, and cold-open the
    derived three-page report. The populated-render receipt must bind both the
    reset baseline/cache hash and the validated derived report recipe/output.

    ```powershell
    .\scripts\capture-phase0-windows.ps1 `
      -CaptureColdRender `
      -DesktopExecutable "C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe" `
      -DesktopVersion "2.146.705.0"
    ```

    The capture fails if the three page screenshots are byte-identical, if the
    rebuilt report hash differs from the linked ten-run output, if either cold
    validator receipt is missing, or if Undo does not restore the placeholder
    baseline exactly.

If the Microsoft validator cannot operate with locally available schemas while
offline, real mode stays blocked. Do not silently drop that validation layer.

Do not hand-author environment, validator, model, offline, kill-test, or
Desktop-render success JSON. The capture code writes
immutable receipts beneath `evidence\phase0`; attestation accepts their hashes
only after independently checking the command contracts and all cross-links.
`config\phase0-attestation.example.json` is an inventory template, not proof.
Populate its references from generated receipts only, then accept and
immediately re-verify the content-addressed operator record:

```powershell
.\scripts\attest-windows-demo.ps1 `
  -AttestationPath .\evidence\gates\phase0-windows.json
```

Attestation is a separate, two-step authority boundary. It binds an operator's
gate assertions to exact artifact hashes; it is not a cryptographic code
signature. Preparation alone can analyze and explain, but cannot enable real
report writes or Desktop launch.

## Launch

Once Gate 0 is evidenced, accepted, and recorded as passed in the decision log:

```powershell
.\scripts\Launch-AI-PowerBI-Analyst.cmd
```

Use `scripts\Reset-AI-PowerBI-Analyst.cmd` before each rehearsal. Reset moves
the prior workspace and runtime state into a timestamped recoverable backup;
it never falls back to a broad delete. Keep Power BI Desktop closed until the
final reveal action has unloaded Ollama and verified the release.
