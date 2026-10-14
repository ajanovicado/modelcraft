# Production readiness audit — September 21, 2026

Status: **development candidate; release blocked**. This audit improves the
current Studio runtime and packaging. It is not clean-machine certification or
proof of complete Power BI compatibility.

## Changes made

- Removed rejected 2B and 0.8B runtime selection branches. Standard now means the
  text-only Qwen3.5 4B runtime; the supported 1.5B Coder profile remains the
  deliberate low-memory fallback.
- Corrected native-engine packaging to preserve the Ollama library directory
  structure and include both supported profiles.
- Frozen inference verifies the installed engine executable's seal, rather than
  looking for Python source files that do not exist in the installed application.
- Moved installed-runtime locks to a writable per-user directory shared by
  profiles and installations.
- Payload assembly verifies developer profile seals and refuses a nonempty
  output directory, preventing accidental mixing with an earlier bundle.
- Added a source inventory and engine build receipt. Packaging rejects a changed
  executable, changed/deleted source inputs, or newly added source files. The
  sidecar build refuses promotion if its inputs change during compilation.
- Updated memory diagnostics, current runtime documentation, and packaging
  commands. Restarted the development server through the supported package CLI.
- Fixed backend lint/format findings and added formatting and frontend lint to
  the release verification script. Validation errors now preserve the underlying
  validator issues for diagnosis.

Historical benchmark records and the separately documented legacy Streamlit demo
remain. They are not active Studio model choices. Removing historical evidence
would not establish that production paths are correct.

## Verification

| Check | Result |
| --- | --- |
| Backend Ruff lint and formatting | Pass |
| Frontend unit tests | 104 passed |
| Frontend ESLint | Pass |
| TypeScript and Vite production build | Pass; large Vega embed chunk warning remains |
| Rust `cargo check --locked` | Pass |
| Python dependency audit | No known vulnerabilities reported; editable projects excluded |
| npm dependency audit | No known vulnerabilities reported |
| Runtime packaging tests | 8 passed, including stale engine/source rejection |
| Runtime/adaptive/guard focused tests | 64 passed before the additional build-receipt tests |
| Backend suite | 1,003 passed, 2 failed, 3 skipped, 5 subtests passed; both failures corrected and passed in the subsequent 8-test packaging run |
| Windows frozen engine | Rebuilt successfully; source/build receipt verified; starts with isolated state, authenticated ping succeeds, missing token returns 401, test processes shut down |
| Strict Python type checking | **Fail: 255 errors in 23 files** |

The two suite failures were `ModuleNotFoundError` in the packaging test loader
after a script import was introduced during the run. The import was corrected;
all eight packaging tests subsequently passed. This is combined suite and
targeted evidence, not a claim of one uninterrupted final-source release run.
An earlier run had a transient PBIP validation failure; the final full run passed
that case. Its underlying cause was not established, and validator diagnostics
were improved instead of bypassing validation.

The browser retained populated report visuals and existing filters after the
server restart. Chat correctly paused at 2.39 GiB free during the build and
2.98 GiB afterward (below the Light profile's 3 GiB startup floor). A successful
new inference was not established in this audit's final browser checks. Memory
guards were not weakened to obtain a passing result.

Machine-local logs are under `.runtime/production-audit-*`. The audits describe
the installed dependency set at the time of this run, not a guarantee of future
security. The third-party editable runtime still needs its own review.

## Outstanding release blockers

1. Resolve strict type errors without disabling the checks. Findings include
   missing annotations, incomplete external-runtime stubs, optional-value use,
   and iterator/interface mismatches.
2. Replace the editable `../PowerBI-RAG/runtime` dependency with a reviewed,
   reproducible dependency available to a clean checkout. The new build receipt
   detects changes in this dependency on this machine; it does not distribute it
   or make CI independent of the sibling checkout.
3. Qualify the signed installed application on clean standard-user Windows,
   without this repository or development tools. Verify native startup, bundled
   tools, upgrade/uninstall, offline isolation, and Application Control acceptance.
4. Complete the current model corpus, numerical goldens, Desktop roundtrips and
   failure-recovery qualification in `release-checklist.md`.
5. Measure sustained inference and memory on the final package. The current
   runtime guard preserves 1.5 GiB; the release performance gate requires at least
   2 GiB headroom. Passing a single request does not satisfy that gate.
6. Establish a reviewed source commit and final immutable release manifest. This
   working tree has not been established as a reviewed release commit.

Do not advertise production readiness until the release checklist has evidence
for every applicable gate. Do not treat skipped Windows privilege tests or a
same-machine executable launch as clean-machine acceptance.
