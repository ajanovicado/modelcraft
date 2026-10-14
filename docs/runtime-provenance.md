# Guarded runtime provenance and clean installs

The project pins `powerbi-rag-runtime==0.1.0` to the noneditable source directory
`vendor/powerbi-rag-runtime`. `uv.lock` records a directory dependency inside the
checkout. A fresh install no longer needs `../PowerBI-RAG/runtime`.

The vendor README and `SOURCE_MANIFEST.json` describe the source capture and
record every upstream file's size and SHA-256. `.gitattributes` preserves those
bytes on Windows and Unix checkouts. The snapshot contains only the reviewed
inference runtime and typing/package metadata; experiments, models and private
evidence are excluded. The package's upstream bytes remain unmodified.

Verify the lock in a fresh environment without changing the normal development
environment:

```powershell
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:TEMP ('studio-runtime-' + [guid]::NewGuid())
uv sync --frozen --all-extras
& (Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts/python.exe') -m pytest tests/unit/test_vendored_runtime.py tests/unit/test_editing_packaging.py tests/unit/test_studio_api_release.py -q
Remove-Item Env:UV_PROJECT_ENVIRONMENT
```

The tests check the content allowlist, reviewed bytes, locked source, installed
noneditable distribution, guard rejection and process-free server lifecycle.
They do not download or start a model. A new runtime snapshot requires repeating
the source review, manifest, fresh install and tests.

The source was captured without license or author metadata. On 2026-10-14 the
project owner confirmed that they created and own it and explicitly authorized
MIT licensing for the public GitHub project. The vendor `OWNER_CONFIRMATION.md`
records that statement, and its MIT `LICENSE` accompanies source and compiled
runtime copies. The original repository URL and revision remain unknown;
ownership confirmation is not an upstream release attestation. External
dependencies and models retain their own licenses.

Existing runtime profiles and sidecar build receipts may name the old absolute
installation path. Rebuild and reseal them from the final installed source;
do not remove path/hash checks or reuse a stale build receipt to accept the new
location. A successful clean install does not complete signing, clean-machine
acceptance, model qualification or the other release gates.
