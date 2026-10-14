# Studio release gates

This replaces the **legacy demo rehearsal checklist for Studio releases**. The old
checklist remains relevant only to Streamlit's fixed Sales & Margin demo. A green
unit suite or a successful Vite build does not approve a Windows release.

## Reproducible engineering run

From the repository, with the locked environment installed:

```powershell
./scripts/verify-release.ps1 -Browser -Native
# Separately, with Power BI Desktop closed:
./.venv/Scripts/python.exe scripts/qualify-studio-desktop.py
```

Evidence goes into a new temporary directory, never into a working user project.
The runner captures source hashes, per-check logs and exit codes, JUnit results,
and timing, and rejects source changes during the run. No Git staging or commit
is performed. Establish a reviewed release commit before distribution.

Browser tests start dedicated engines on 18765 and Vite on 15173 with isolated
state and an explicitly allowed development origin. They use installed Edge
through Playwright. They do not attach to 5173/8765 or overwrite saved workspaces.
The native origin and production engine handshake are tested separately.

The same `-Browser` run also includes the fixture-only chat regression on an
isolated Vite server at 15174. Every API request is mocked; this catches selected
context, failed-request recovery, and chat/designer interactions without a model,
backend, Power BI installation, or changes to saved user workspaces.

The `-Browser` gate also runs `test:data-browser` on isolated Vite port 15175.
It covers direct-source setup, retry/reimport, prerequisites, exact preview IDs
and native DAX without local rows. Database and Desktop operations are mocked;
it never queries a real account or changes saved user reports.

`-Native` and the Rust CI job use the explicit debug-only `source-check` feature.
It compiles the native code without copying model/engine payloads and cannot run
as an app or build in a release profile. This checks native source compilation,
not bundle qualification. Normal builds retain the required runtime and sidecar
resources; verified payload staging, sealed receipts, installer building and
installed-app acceptance remain separate gates.

## Developer chat smoke and semantic review

With the configured guarded runtime available, run a small real-model discussion
probe on a quiet host using fresh isolated state:

```powershell
./.venv/Scripts/python.exe scripts/evaluate-studio-developer-chat.py --list-cases
./.venv/Scripts/python.exe scripts/evaluate-studio-developer-chat.py --output-dir "$env:TEMP/studio-developer-review" --case decorators --case decorators-example
# Omit --case to run all six built-in cases.
```

`--case` is repeatable; follow-ups include their prerequisite once and share that
isolated conversation. `--output-dir` is required, and each run gets a new
directory. The runner never opens saved user workspaces, executes generated code
or reseals a runtime.

Review `summary.json`, each case's `result.json` and `response.md`, and the native
generation evidence. Check relevance, correctness, examples, concision and finish
status: an ended API iterator or valid Python AST does not prove that an answer is
complete or correct. Record the semantic review separately; automatic checks and
exit zero are smoke results. These six developer-discussion cases do not replace
the current Studio model corpus, grounded imported-data tests or Desktop gates.

## Advertised release scope

| Capability | Release requirement |
| --- | --- |
| CSV/XLS/XLSX/XLSM/Parquet import | Signature/encoding, quoted CSV, nulls, formulas, macro non-execution, large bounded reads, source hashes |
| SQL Server/Azure SQL and Snowflake snapshots | Packaged SDK loading; supported native ODBC driver, real configured profiles, TLS/authentication/read access, preview/import/restart/reimport, limits, exact numeric IDs/decimals and UTC findings |
| Power BI-managed sources | Qualify each advertised connector and mode in Desktop; distinguish source refresh, saved-row extraction and model query; require exact working-copy engine, permissions, credentials and any service gateway |
| Profiles and relationships | Full-snapshot key evidence; ambiguous/nonunique/many-to-many cases fail closed |
| Chat and revisions | Real pinned local model; progress, interruption, cancellation, follow-ups, capability honesty |
| Measures | Numerical goldens including counts/blanks, ratios, dates, supported LOD; advanced DAX separate approval |
| Designer | Actual source-backed previews; unsupported local calculation clearly deferred; stale bindings cannot display old results |
| New PBIP projects | Both validators; refresh in Desktop; every page inspected and measured |
| Managed rebuild | Workspace-isolated target, approved change, base-hash guard, operation lock, exact rollback and undo |
| Existing PBIP | Compatibility reference / separate managed copy only; no arbitrary in-place migration claim |
| Export | Byte-exact copy on the same machine; managed source paths remain local, not a portable cross-machine data package |
| Native visuals | Publish only the exact qualified offline visual IDs; no blanket all-native-visual promise |
| Installed application | Standard-user clean Windows install, startup/restart, offline operation, upgrade/uninstall without data loss |

## Blocking gates

- [ ] Reviewed source commit, dependency locks, model/tool identities and release manifest.
- [ ] Backend suite, static checks, frontend tests and browser scenarios pass on the final source manifest.
- [ ] Skipped privilege-dependent path/symlink tests run on an appropriate Windows test host.
- [ ] Current Studio model corpus run three times; schema >=98%, supported task success >=90%, zero critical hallucinations/invented accepted IDs. Legacy router scores are not Studio model scores.
- [ ] Measure/relationship goldens agree between source, local preview and Desktop; blanks and unknown dimension members are included explicitly.
- [ ] Ten consecutive build/validate/Desktop-render cycles pass. Five new plus five managed rebuilds do **not** qualify in-place migration.
- [ ] Inject source copy, compilation, validation, promotion, receipt, export, undo and hard-kill failures; verify exact recovery, including restart journaling.
- [ ] No unapproved writes, stale approval bypass, cross-workspace overwrites, unsafe source traversal or non-loopback model endpoint.
- [ ] Installed app works without repository, Python, Node or Rust; required Power BI CLIs are bundled or explicitly provisioned and verified.
- [ ] Repeated and concurrent native launches, including while minimized or responding, retain one app window and one engine process. The existing window is shown and focused, current chats/typed text/draft state stay intact, and a clean close or forced termination permits a fresh launch. Record process counts and installed-app evidence; Rust source checks do not qualify this behavior.
- [ ] Installer/GUI/engine signing and Windows Application Control acceptance; no security bypasses.
- [ ] Setup handles missing WebView2, Power BI, Ollama/model, interrupted setup and digest mismatch.
- [ ] Complete the [data source qualification matrix](data-source-connections.md) with actual approved connections and installed-app SDK/driver evidence. Fixture tests do not prove tenant sign-in, DirectQuery/live-model support or Desktop source refresh; Desktop model processing automation requires version-specific acceptance.
- [ ] Qualify every advertised [model storage mode](model-storage-modes.md): Import, DirectQuery, Dual, Hybrid and Direct Lake; mixed modes/source groups and live/thin reports need separate evidence. Compare native values with Desktop, include stale caches and incremental refresh boundaries, and verify missing/ambiguous/stale working-copy engines cannot supply another report's values.
- [ ] Runtime network isolation tested at OS level; browser request checks alone are not complete offline proof.
- [ ] Performance measured without competing build tools: cold/warm startup, first response, cancellation, throughput, import sizes, memory and leak soak. Preserve >=2 GiB system headroom; prefer >=8 tokens/sec, investigate 6–8, fail default-model gate below 6.
- [ ] License/dependency security review, release notes and support/recovery instructions.

Never convert missing evidence into a pass. Signing, clean-machine acceptance and
the expanded model corpus remain independent of the automated engineering runner.
