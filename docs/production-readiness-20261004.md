# Engineering readiness audit — October 4, 2026

Status: **development candidate; distribution is not approved**. This audit
records source checks, isolated browser regressions, reproducible dependency
installation and remaining release requirements. Mocked browser results do not
qualify the local model, a Power BI render or an installed Windows application.

## Changes and behavior

- Workspace views and mixed chat-history scopes share a compact yellow selection
  track with stable labels, native keyboard navigation and reduced-motion support.
  Native detail disclosures reveal smoothly on supporting engines and retain
  their instant native fallback elsewhere. Composer hints and copy confirmation
  are more legible. Escape only closes history whose panel contains focus; opening
  an archived Workspace chat focuses its read-only transcript.
- DAX measure actions reserve their button width and show progress only on the
  clicked action. A real callback confirms Saved for draft proposals or Queued
  for imported pending changes; it does not claim Power BI application or formula
  validation. Synchronous admission and owner/candidate/form guards reject double
  submission and stale callbacks. Failures stay beside their retry control.
- Source connections distinguish local files, direct SQL Server/Azure SQL and
  Snowflake snapshots, and Power BI-managed connectors. First launch supports a
  database workspace without a folder. Repeat import restores its exact source
  settings and row limit for review; failed imports preserve their request
  identity, while completed imports with failed screen loading retry only the
  load. The Sources footer explicitly scopes snapshot mode to direct database
  imports and directs native connections to Power BI Desktop.
  See [source connections and qualification](data-source-connections.md).
- Connector prerequisite checks load the SDK and verify SQL Server's modern
  native ODBC driver separately from profile authentication. Immutable copied
  bytes drive profiling and normalization. Regression coverage includes nullable
  large IDs, exact decimal text, signed-integer overflow rejection, duplicate
  headers, UTC timestamp findings, source changes and failed extraction cleanup.
  Table previews retain integer digits without expanding ordinary decimal
  display; values outside JavaScript's safe integer range travel as text.
- Imported native DAX can run without saved local rows when the exact Desktop
  working-copy engine is available. Model measure → DAX candidate → explicit
  preview is connected through the real App. AI generation/local evaluation
  still need a workspace snapshot. Recognized native-only business patterns can
  use current metadata for names-only proposals, with no computed values.
  This is not universal remote/live-model connectivity.
  Generated-project hydration also binds the verified Desktop child model port
  to a named connection, rejecting unrelated or changed processes before refresh.
- Imported model storage is read from current table/partition metadata:
  Import, DirectQuery, Dual, Hybrid, Direct Lake and mixed storage. The collapsed
  Model storage summary and table badges separate mode from saved-row coverage.
  Missing metadata and no-local-model reports do not imply Import or a verified
  live connection. See [model storage modes](model-storage-modes.md).
- DAX, visuals and computed chat answers refuse snapshot evaluation of native-only
  or incomplete referenced tables. Current imports and attempted model reads
  persist a read marker, so failure cannot silently return to the legacy local
  engine after restart. Untouched pre-metadata imports retain their legacy
  snapshot behavior until reread; those results do not establish mode or freshness.
  Formula proposals use current names and reject stale dependencies.
- PBIP compatibility follows the selected report's linked semantic model and
  structured storage properties. Non-Import modes, composite models and custom
  unverified data-source functions stay outside managed-project migration.
  Native DAX rechecks the exact working-copy model before connecting and returning
  results; absent or ambiguous engines fail without a remote XMLA fallback.
- Sidecar build configuration installs the frozen database extra and collects
  Snowflake's connector and pyodbc. The actual packaged SDKs and external ODBC
  prerequisite still need installed-app qualification.
- One visible Local AI panel identifies Workspace or DAX Lab context. Shared,
  searchable history lists both scopes, while their model context stays separate.
  Explicit formula handoffs retain the original request and a source-return link
  across reloads. See [assistant conversations](assistant-conversations.md).
- Main and DAX chats have visible Chat history and New chat controls. Saved chats
  are searchable and read only; returning to the current chat preserves the
  typed question. New chat preserves typed text, and DAX retains its formula.
- DAX history is reconstructed from workspace events after browser history
  caches are removed. Archived dialogue does not become context for a new chat.
  Stopped partial responses are labeled and cannot apply a formula.
- Failed or interrupted requests retain their original prompt, target, base
  hash and request identity through reload. Retrying does not replace a later
  question or separately edited DAX formula. A stale write still requires the
  server's base-hash check.
- Conversation requests capture the current chat boundary. Archived receipts,
  stale recovery data and workspace saves cannot restore an earlier chat.
  Only the reserved request and exact input can commit an imported edit receipt.
  Completed engine responses outrank leftover browser retry records; incomplete
  replies remain readable but cannot become completed formula handoffs.
- The engine owns its state folder with a process-lifetime OS lock. The native
  app registers the Single Instance plugin before starting its sidecar, restoring
  the existing window on a second launch. This uses exact-pinned plugin 2.4.5;
  Tauri remains 2.11.5 and existing crate versions/checksums were preserved.
  Actual installed-app second-launch behavior still requires qualification.
- Chat responses support readable code blocks with Copy code, ordered steps and
  tables. Forecast numeric errors remain visible and block Save; invalid date
  bindings explain why forecasting is unavailable.
- The guarded runtime is pinned to a noneditable source snapshot inside this
  checkout. Its nine upstream files are byte-identical, content hashed and
  checked against the installed package allowlist. No sibling checkout, models,
  experiments or private evidence are needed to install the Python dependency.
  See [runtime provenance](runtime-provenance.md) for the remaining permission
  requirement.
- Targeted security updates change brace-expansion 1.1.18→1.1.21 and
  5.0.9→5.0.12, PyJWT 2.14.0→2.15.0, and urllib3 2.7.0→2.8.0. Other dependency
  versions were preserved. CI installs the frozen Python lock.
- Rust CI and the engineering runner use an explicit debug-only `source-check`
  feature to compile without copying release payloads. It cannot run as an app
  or be used in a release profile. Normal native builds still require the
  engine and runtime resources; no placeholder payload was staged.

## Verification

| Check | Result |
| --- | --- |
| Interface polish frontend gate | 531 passed, zero failures or skips; ESLint and TypeScript/Vite build pass; 860.69 / 985.57 kB JS chunk warnings remain |
| Interface polish chat/source regression gate | Five chat and three source/storage browser scripts passed after the tab/history/action changes; the history runner now covers cross-scope Escape, preserved hidden search/filter state and archived-transcript focus |
| Interface polish rendered QA | Twelve checks pass at 1440/1280/800/390 px: native keyboard/focus, shared history routing/search, all five disclosure classes, rapid reversal/closed focus, reader scroll, actual clipboard feedback, real callback pending/error/exact retry/receipt and reduced motion; zero unhandled application requests or runtime/console errors |
| Frontend unit tests | 520 passed after model storage changes, zero failures or skips |
| Frontend ESLint | Pass |
| TypeScript / Vite production build | Pass after model storage and Sources guidance changes; 860.69 / 980.61 kB JS chunk warnings remain |
| Model storage combined backend gate | 1,254 passed, 5 subtests passed in 175.90 s: model modes, unknown/read markers, local/native DAX, pattern proposals, calculated chat answers, imported edit lifecycle, conversations, selected PBIP compatibility, source routes and builder regressions; all state isolated |
| Model storage static gates | Strict mypy on fourteen affected source files; scoped Ruff and formatting pass |
| Model storage browser scenarios | Pass at 1440×900 and 1280×884: collapsed details, table badges, mixed modes, partial facts, reading state, no local model and saved-row coverage; all API operations mocked |
| Live app after model audit reload | Authenticated engine ready, model available, Desktop available; existing Superstore workspace and chat history restored, Data navigation works, no overlay or fresh console errors at 1864×884 |
| Source connection backend regressions | 266 passed across ingestion, inventory, SQL/Snowflake, exact browser values, native report/DAX routing, Desktop hydration, saved report extraction and packaging; seven source-route cases subsequently rerun with end-to-end UTC findings |
| Source connection static gates | Strict mypy on nine affected source files, scoped Ruff/format on sixteen files, frontend lint/build and diff checks pass |
| Source connection browser scenarios | Pass at 1440×900 and 1280×884: prerequisite retry, native-driver guard, stale/duplicate request rejection, query-free reimport, exact request replay, reload-only recovery, large IDs, connector help, folder-free first run and full-App imported measure → native DAX preview; all database/Desktop operations mocked |
| Live source screen after reload | Authenticated engine ready, model available, Desktop available; Snowflake SDK loads, SQL prerequisite correctly reports missing Microsoft ODBC Driver 17/18; Sources and connector guidance inspected at 1864×884 with no overlay or fresh console errors |
| Isolated chat browser scenarios | Pass at 1440 and 1280 px; every API request mocked |
| Forecast browser scenarios | 57 mocked requests per width; three in-memory saves and failed/successful recovery |
| Main / DAX history browser scenarios | 90 mocked requests per width; reload, archive, Copy code, retry and stopped partial response checks |
| Shared assistant handoff browser scenario | Pass at 1440×900 plus 800/390 px; 48 mocked requests, original-request grounding, source return, reload/detach, handoff-only New chat and long-thread scroll preservation |
| Conversation lifecycle browser scenarios | Pass: duplicate-send admission, delayed transcript reconciliation, storage failure, durable completion/retry authority, failed DAX context exclusion, changed-source candidate review and late-reply rejection after New chat; every API request mocked |
| Conversation audit backend regressions | 879 passed: 381 conversation/state/draft/imported-edit tests plus 498 DAX tests; isolated state and fake model/Desktop |
| Engine state-owner regressions | 12 passed: concurrent startup, normal/abrupt/forced exit, pre-service rejection and token-safe errors; active development engine also rejects a second owner |
| Conversation audit static checks | Strict mypy on six changed source files, scoped Ruff/formatting, frontend lint/typecheck and diff checks pass |
| Live app after source reload | Authenticated health is ready, model available; saved Workspace/DAX histories restored, one assistant visible, no framework overlay or fresh browser console errors |
| Assistant follow-up DAX regressions | 498 passed across compiler, request routing and existing DAX flows; fixed totals cannot be substituted with shares |
| Workspace/styling follow-up regressions | 55 passed, 5 subtests passed |
| Frozen fresh Python environment | 120 packages installed, initially offline; security updates then installed from the same frozen lock |
| Runtime / packaging / release API tests in fresh environment | 101 passed |
| Vendor wheel inspection | Eight package files plus standard metadata only; every package byte matches the snapshot |
| Runtime smoke imports | Installed site-packages runtime, managed backend and text-only backend import successfully |
| Strict Python typing | Pass: 197 files including the developer smoke runner; no ignores added to silence application errors |
| Python lint and formatting | Pass: source, tests and local third-party API typing stubs |
| Earlier full backend suite | 7,355 passed, 3 Windows symlink-privilege skips, 5 subtests passed; 857.04 s with parallel workers; conversation audit changes subsequently covered by the focused gates above |
| Final developer routing regressions | 246 passed after the combined suite |
| npm audit | Zero known vulnerabilities reported |
| Python dependency audit | No known vulnerabilities reported in audited packages |
| Rust source check / Clippy / formatting | Pass with locked debug source-check configuration |
| Native Single Instance source gates | Locked source check, all-target Clippy with warnings denied and formatting pass; second-launch focus/process behavior remains an installed-app gate |
| Native payload rejection | Normal build rejects the missing runtime directory; release source-check is rejected before bundling |
| Real local-model programming probe | `safe_divide` response in 30.83 s, valid Python AST, no report mutation |
| First six-case developer probe | Mechanical checks passed; assistant source review found 3 acceptable cases and 3 requiring correction |
| Second six-case developer probe | Mechanical checks passed; native generations ended normally; assistant source review found 4 acceptable cases and 2 precision gaps |
| Final-source six-case developer probe | Mechanical checks passed; native generations ended normally; assistant source review found 4 acceptable cases and 2 remaining model-quality gaps |

The combined backend run emitted one expected thread warning from an injected
hard-kill case. The first six-case developer probe passed mechanical checks but
found a semantic failure: a long decorator explanation exhausted the history
budget, so the subsequent “Show an example” lost its topic and returned an
unrelated example. Review also found ambiguous decorator-order advice and a dict
example that omitted the requested bracket read and optional-key lookup. The
saved `semantic-review.json` is explicitly assistant source inspection, with
overall semantic and qualification results false; it is not human qualification.
The second six-case probe retains the original decorator topic in both prepared
and native follow-up history and returns a relevant decorator example. Four
answers are acceptable by assistant source inspection; two precision gaps remain:
the dict example still omits the requested operations, and the SQL example
asserts join-result rows without supplying the joining key values. Its separate
`semantic-review.json` records overall semantic and qualification results false.
A final compact technical prompt passed 246 focused regressions and was probed
with the same six inputs. All mechanical checks passed, every native generation
ended with `complete=true` and `finish_reason=stop`, and isolated workspace hashes
stayed unchanged. Source inspection finds four acceptable answers and two open
model-quality gaps: the dict response still omits the required bracket read and
optional lookup, and the decorator explanation expands beyond the requested
concision into an unsolicited implementation and extra advice. The final
follow-up retains its topic, and the SQL answer no longer asserts unsupported
result rows. The third review records overall semantic and qualification results
false; these remaining gaps require model qualification rather than a claim that
all generated answers are correct. The programming probe checks a proposed answer's syntax
and mutation isolation, not execution correctness or model-corpus qualification.

The reusable real-model probe is `scripts/evaluate-studio-developer-chat.py`:

```powershell
./.venv/Scripts/python.exe scripts/evaluate-studio-developer-chat.py --list-cases
./.venv/Scripts/python.exe scripts/evaluate-studio-developer-chat.py --output-dir "$env:TEMP/studio-developer-review" --case decorators --case decorators-example
```

Repeat `--case` to select probes, or omit it for all six built-in cases. A
follow-up includes its prerequisite and shares one isolated conversation. Each
run creates fresh state under the explicit output directory, records prompts,
responses and native evidence, and requires semantic review of `response.md` and
the finish status alongside `result.json` / `summary.json`. It never executes
generated code or reseals a runtime. Exit zero is an automatic smoke result;
meaning, behavior and relevance still require review. This is separate from the
full Studio corpus and imported-model qualification.

The Python audit explicitly excludes the editable application and cannot query
the non-PyPI vendored runtime. Its five source modules were reviewed and their
bytes are verified, but a vulnerability-database skip is not an audit pass for
that package. Security results describe the installed versions on this date.

Browser scenarios run on dedicated Vite port 15174 using fixture-only state.
They never call the user's engine, mutate saved workspaces, start a model or
launch Power BI. Screenshots at both desktop widths were inspected for visible
controls and overlap. Logs and screenshots remain outside the source tree.

The source connection regression runner uses separate Vite port 15175 and a
dummy API target at 19877; every API operation is mocked. Source audit evidence
does not establish authenticated Snowflake/SQL access, Desktop source refresh,
DirectQuery/live-mode support, or packaged SDK execution. The live check only
read existing local rows and connector prerequisites. No source accounts or
user reports were refreshed during this audit. The development runtime's 257
existing source pins were resealed during the source audit. The later model audit
resealed those pins and added the native proposal metadata helper (258 pins total),
then restarted the development engine and verified authenticated readiness and
the live app. These are developer reloads, not new source-frozen release
qualification. Model storage checks exercise saved files and synthetic metadata;
they do not establish real DirectQuery, Direct Lake or live-model results. The
five existing chat browser scripts and all three source/storage browser scripts
also passed after these edits, with every API operation mocked.

The interaction fixture used separate port 15176 and controlled callback receipts.
Its development HMR socket handshake was mocked after browser local-network checks
rejected the fulfilled test document; browser security settings were unchanged.
Final evidence records that fixture transport, zero application console/runtime
errors and nine matching polished-module hashes. The server was stopped after QA.
The later App callback audit changed missing draft/table branches to reject rather
than silently resolve; frontend tests, lint/build and all eight chat/source browser
scripts passed again afterward. No live model, database or Desktop action was run.
The existing live app at 5173 restored its saved chats after reload and had no
overlay or fresh console warnings/errors at 1864×884. These checks qualify the
interface behavior, not installed WebView2 or authenticated source connections.

## Remaining release requirements

1. Establish the vendored runtime's owner, source provenance and redistribution
   permission. No license, copyright holder or repository revision was supplied;
   the application's MIT license does not establish a license for that source.
2. Freeze a reviewed source commit, rebuild the engine and runtime payload from
   that source, and verify current profiles and source/build receipts. Stale
   receipts and old absolute installation paths must not be accepted.
3. Build and sign the actual installer, then qualify installation, startup,
   restart, offline use, upgrade and uninstall on a standard-user clean Windows
   machine without the repository or development tools.
4. Run the current real local-model corpus and native Power BI validation/render
   cycles required by the [release checklist](release-checklist.md). Isolated
   mock tests and compiler checks do not complete these gates.

Run `scripts/verify-release.ps1 -Browser -Native` for recorded engineering
checks, and the separately documented Desktop qualification after payload
staging. Preserve all signing, source/hash, approval, stale-write and rollback
checks during release qualification.
