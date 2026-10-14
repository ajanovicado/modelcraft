# PowerBI-RAG runtime snapshot

This is the inference-only `powerbi-rag-runtime` 0.1.0 package used by the guarded
local backend. It is installed as a noneditable, locked directory dependency so a
fresh checkout does not require a sibling `PowerBI-RAG` directory.

The nine upstream files listed in `SOURCE_MANIFEST.json` were copied byte for byte
on 2026-10-04 from the local `PowerBI-RAG/runtime` directory. The source directory
was not a Git checkout. No repository URL, revision, author, copyright holder or
license was supplied in its metadata, and no project-owned license was found.
The manifest's content digest identifies this local snapshot; it is not an
upstream release attestation.

On 2026-10-14 the project owner confirmed that they created and own this runtime
and explicitly authorized **MIT licensing** for the public Modelcraft project.
See `OWNER_CONFIRMATION.md` and `LICENSE`. Source and compiled copies may be
distributed under that license. Third-party dependencies retain their own
licenses. Ownership confirmation does not complete installer signing or release
qualification.

Only five Python modules, two typing stubs, `py.typed`, and upstream package
metadata are included. No model weights, native backend binaries, credentials,
training code, experiment results, logs, test data or private evidence were copied.
The unused `backend.pyi` is preserved from the supplied package; it does not
provide a `backend.py` implementation or a native model binary.

The guarded runtime sources are intentionally unmodified. Snapshot integrity is
checked by `tests/unit/test_vendored_runtime.py`; upstream code is excluded from
project formatting to preserve these hashes. Package dependencies remain in
the project's `uv.lock` and keep their own licenses.

Changes to this snapshot require a new provenance review, manifest hashes,
noneditable package rebuild, frozen-install verification and runtime profile /
sidecar receipt resealing. Existing absolute-path build receipts do not attest
this new installation.
