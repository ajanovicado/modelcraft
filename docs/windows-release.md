# Windows candidates and downloadable releases

Modelcraft and its vendored inference runtime are MIT-licensed source. A public
GitHub portfolio can show the source, architecture, screenshots and CI results
without a signing certificate or installer release. Python, npm and Cargo locks
support a fresh source checkout. The normal CI workflow runs frozen installation,
backend checks, frontend tests/build, isolated fixture browser scenarios, and a
debug-only native `source-check`; these checks do not start a model or require
packaged runtime files. `source-check` cannot run the application.

Windows installers have additional gates. Keep source publication and claims of
qualified Windows distribution separate. See [release-checklist.md](release-checklist.md)
for the required real-model, Desktop, recovery and installed-app evidence.

## Prepare a portable runtime input

On the development machine with the reviewed, sealed Qwen 4B profile, its exact
model bytes and native runtime files available:

```powershell
uv sync --frozen --all-extras --python 3.12.12
uv run --frozen python scripts/build_store_payload.py --export-input --out .runtime/runtime-input-0.1.0
```

Use a fresh output directory every time. Export verifies the existing source,
model and payload pins. A mismatch requires a reviewed rebuild/reseal; export
does not silently accept changed inputs. The resulting `input-manifest.json`
binds portable runtime files and repository-relative source hashes. It includes
the inference server, required libraries, retrieval corpus, validator assets and
Node, but excludes model weights and Python source bytes. First launch installs
the exact pinned model from a local file or user-started download.

Portable input can be supplied to another checkout of the same reviewed source.
Import rejects modified, added, escaped or stale files/source pins. Record the
manifest SHA-256 before moving it. This input is a build dependency, not a user
installer or a model-quality qualification receipt.

## Build an isolated local candidate

Run PowerShell 7 on Windows x64 with Git, uv, Node 22, Rust/MSVC and the Windows
build prerequisites available. The bundled tool builder also uses the Windows
.NET Framework C# compiler. Node 22.22.3, Rust 1.98.0, uv 0.11.19 and Python
3.12.12 are the pinned release-workflow toolchain.

```powershell
./scripts/build-windows-release.ps1 `
  -RuntimeInput .runtime/runtime-input-0.1.0 `
  -OutputDirectory .runtime/candidate-0.1.0 `
  -AllowUnsigned -AllowDirty
```

`AllowUnsigned` explicitly labels an unsigned local candidate. `AllowDirty`
allows a hash-recorded snapshot of current work for local verification; released
candidates require a clean reviewed commit. `SkipEngineeringChecks` is available
for packaging diagnostics and records incomplete engineering qualification.
None of these switches grants distribution approval.

The builder verifies version agreement across Python/npm/Cargo/Tauri manifests
and locks, copies source into a fresh output tree, installs frozen dependencies,
runs engineering checks, builds the engine and self-contained Power BI tools,
assembles the runtime and creates NSIS/MSI installers. Existing developer runtime,
engine, installers and user workspaces are not replaced. Output directories must
be new; outputs inside the checkout must be Git-ignored.

Power BI Desktop remains a separate user installation. The report tooling ships
as native launchers plus frozen pbi-cli, Node and locked Microsoft authoring/bridge
packages. Their manifests are verified and pinned in the runtime profiles.
Launchers reject redirected payload paths and assign a suspended child to a
process job before execution, so termination also ends its process tree.

To reuse a separately built toolchain:

```powershell
./scripts/build-powerbi-tools.ps1 -OutDirectory .runtime/powerbi-tools-0.1.0
./scripts/build-windows-release.ps1 `
  -RuntimeInput .runtime/runtime-input-0.1.0 `
  -ToolchainRoot .runtime/powerbi-tools-0.1.0 `
  -OutputDirectory .runtime/candidate-tools-0.1.0 `
  -AllowUnsigned -AllowDirty
```

`AllowPartialTools` skips automatic tooling assembly only for an explicitly
incomplete local candidate. A candidate without the tools cannot pass the draft
release gate. A successful build places installers, checksums and source/candidate
manifests in `<output>/artifacts`; logs and the copied build tree stay under the
output directory. `candidate.json` always records `release_approved: false`.

## Sign and qualify installers

Provision a code-signing certificate with its private key in
`Cert:\CurrentUser\My`, Windows SDK SignTool and an RFC 3161 timestamp service.
Then omit local exception flags:

```powershell
./scripts/build-windows-release.ps1 `
  -RuntimeInput .runtime/runtime-input-0.1.0 `
  -OutputDirectory .runtime/signed-candidate-0.1.0 `
  -CertificateThumbprint '<40-character certificate thumbprint>' `
  -TimestampUrl '<RFC 3161 timestamp service URL>' `
  -SignToolPath '<absolute path to signtool.exe>'
```

The engine is signed before finalizing its source/build receipt. Tool binaries
are signed before their launcher manifests are sealed. Native inference resources
are verified against portable input, existing trusted vendor signatures are
preserved, unsigned resources are signed, and runtime/profile hashes are finalized
afterward. Tauri signs the GUI and installers through the same signing helper.
The builder verifies trusted signatures before recording a signed candidate.
It preserves original/signed inference hashes in `runtime-signatures.json`.

Signing does not establish acceptance on a clean standard-user Windows machine.
Run the installed-app, upgrade/uninstall, real-model, Desktop, offline, license and
security gates against the exact candidate bytes. Keep a reviewed qualification
record and evidence references; fixture tests cannot fill these gates.

## Manual GitHub workflow

`.github/workflows/release.yml` has two manual modes and never publishes a release.
Configure the `windows-release` environment with required reviewers and:

| Setting | Purpose |
| --- | --- |
| Secret `WINDOWS_SIGNING_PFX_BASE64` | Exported signing certificate/private key |
| Secret `WINDOWS_SIGNING_PFX_PASSWORD` | PFX password |
| Variable `WINDOWS_CERTIFICATE_THUMBPRINT` | Approved signing identity |
| Variable `WINDOWS_TIMESTAMP_URL` | RFC 3161 timestamp service |

Do not commit certificate files or passwords. A hosted runner cannot reuse the
development machine's absolute runtime paths. Upload the portable runtime input
to an explicitly chosen same-repository workflow artifact, then dispatch
`build_candidate` with the full reviewed source commit, that artifact's run ID/name,
and SHA-256 of `input-manifest.json`. Missing signing prerequisites fail before
any unsigned candidate upload. Successful runs save `modelcraft-signed-candidate`
as an Actions artifact for acceptance testing.

After acceptance, prepare an artifact named `release-qualification` containing
`qualification.json` and reviewed `release-notes.md`. The qualification schema is:

- `schema: 1`, `approved_for_distribution: true`, `source_commit`, `reviewed_by`
  and `reviewed_at`;
- `candidate_manifest_sha256` and `release_notes_sha256` binding the exact files;
- `gates`, with `passed: true` and a nonempty actual `evidence` reference for each
  of `clean_windows_install`, `upgrade_uninstall`, `real_model_corpus`,
  `powerbi_desktop`, `offline_operation`, `licenses_reviewed` and `security_review`.

These values are assertions supported by completed evidence, not a template to
turn missing checks green. Dispatch `prepare_draft` with the signed candidate run
ID/name and `candidate.json` SHA-256, plus the qualification run ID and
`qualification.json` SHA-256. It rejects unsigned/incomplete/dirty candidates,
changed installer bytes, mismatched source/notes and missing gate evidence, checks
installer signatures again, and creates a same-repository **draft** GitHub release.
Publishing the reviewed draft remains a separate action.
