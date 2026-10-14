# Microsoft Store submission checklist

Target: **unpackaged Win32** submission (NSIS/MSI accepted by Partner Center).
App type: desktop, offline-first, no account required.

## Completed in-repo

- [x] Compliant display name: **AI Analyst Studio for Power BI**
      (`productName`, window title, brand, index.html, README)
- [x] AI runtime ships under `<install>/runtime/` via `bundle.resources`: the
      Ollama llama-server tree, worked-example corpus, node.exe and the M
      validator — see `scripts/build_store_payload.py` (verifies every copied
      byte against the seal)
- [x] The one model, **Qwen3.5 4B**, is installed on first launch: it is a single
      3.4 GB file and MSI/NSIS installers stop at 2 GB. The packaged profile pins
      its SHA-256 and registry address; the app installs it into
      `%LOCALAPPDATA%\AI Power BI Analyst\models` from an existing Ollama copy,
      a user-chosen file, or a one-time download, and refuses any other bytes
      (`llm/model_install.py`, `/v1/model/install`)
- [x] Runtime path resolution is install-relative, not developer-absolute:
      `ai_powerbi_analyst.paths` anchors `runtime/catalog.json` at the
      executable directory (frozen) or repository root (dev)
- [x] Fail-closed integrity preserved: packaged profiles pin the payload files
      **and** `../ai-powerbi-studio-engine.exe`; embedded app assets are aliased
      to payload pins so the bytes executed are the bytes verified
- [x] Per-user writable paths only: state root `%LOCALAPPDATA%\AI Power BI Analyst`,
      runtime lock under `<install>/runtime/training/` (NSIS per-user install)
- [x] WebView2 handled: `webviewInstallMode: embedBootstrapper` (no silent
      failure on machines without the runtime)
- [x] `PRIVACY.md` — local-only data statement ready to host
- [x] Loopback-only authenticated sidecar, ephemeral port, per-launch token;
      no production fallback to dev endpoints or tokens
- [x] Graceful degradation for optional dependencies (Power BI Desktop,
      `pbi-cli`, low-RAM devices) already built into health reporting

## Build-time steps (each release)

1. `scripts/build-desktop-sidecar.ps1` → fresh engine exe into `src-tauri/binaries/`
2. `scripts/build_store_payload.py` → rebuild `src-tauri/runtime/` payload
   (re-hashes the engine exe into the profile seal — run after every engine build)
3. `npm run build` + `cargo tauri build` → NSIS/MSI installers
4. Smoke-test the installer on a clean machine/VM:
   - app launches, sidecar connects, `/v1/health` reports `ready`
   - DAX ask → bounded answer; chat → response; export → `.pbip`
   - first launch offers "Install Qwen 4B"; the install finishes, the top bar
     shows "Local AI ready" and a chat answers
   - uninstall; confirm `%LOCALAPPDATA%\AI Power BI Analyst` remains (by design)

## Partner Center steps (account work — cannot be done in-repo)

- [ ] Reserve the app name; the listing name must match `productName`
- [ ] Host `PRIVACY.md` at a public URL (required field for every submission)
- [ ] Complete the IARC age-rating questionnaire
- [ ] Write listing copy + screenshots; state requirements honestly:
      Windows 10/11 x64; a one-time 3.4 GB Qwen 4B download on first launch
      (about 4 GB of disk); Power BI Desktop optional (required only for
      "open in Desktop")
- [ ] Declare capabilities truthfully: file-system access via picker only,
      local loopback server; internet only for the user-started one-time model
      download (registry.ollama.ai), no data leaves the PC
- [ ] Upload the signed NSIS or MSI; Microsoft re-signs on publish
- [ ] Note in submission: app spawns a bundled helper process
      (`ai-powerbi-studio-engine.exe`) and a bundled local inference server —
      both signed/verified, loopback-only

## Known limitations to state in the listing

- The installer does not contain the model; Qwen 4B (the only model) is
  installed on first launch, which needs internet once unless a local copy of
  the pinned file is available. Low RAM makes it slower, never refused.
- Power BI Desktop integration requires a separate Desktop install (Store or MSI).
- The model is CPU-only; first inference after idle can take tens of seconds.
- Internal identifiers retain legacy names (`ai-powerbi-studio-engine.exe`,
  `%LOCALAPPDATA%\AI Power BI Analyst`); display surfaces use the new name.
