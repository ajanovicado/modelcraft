# Public source publication audit

The project owner chose MIT licensing for the application and the separately
vendored inference runtime on October 14, 2026. A public portfolio should start
from a fresh source snapshot. The original development repository and its Git
history are preserved locally; neither is a suitable publication bundle because
they contain historical evidence, machine-specific seals and generated artifacts.

## What was inspected

The publication review inventoried 980 tracked files and four reachable Git
commits. A bounded local pattern scan examined 1,473 tracked and nonignored
untracked text files, and 1,523 unique eligible text blobs across all four commits.
It found no matches for private-key headers, GitHub tokens, cloud access-key IDs,
provider API-key shapes, credentials in URLs or long literal credential
assignments. Values and source excerpts were never emitted by the scanner.

Neither Gitleaks nor TruffleHog was installed. These results describe a custom
pattern scan, not a full secret-scanner certification or proof that every secret
is absent. The history scan excluded 175 binary or over-4-MiB blobs; the worktree
scan excluded 124 binary or large files. Ignored model stores, application state,
build output and files outside the repository were not scanned or exported.

Machine-account paths appeared in runtime seals, private evidence, historical
documentation and several probe/fixture scripts. Actual account names were
replaced with generic examples or current-user/temp paths in the retained source
fixtures. The exporter additionally redacts Windows account-path examples in
Markdown copies without modifying the original documentation.
Historical Markdown records containing those paths also receive a short notice
that referenced machine artifacts are excluded and current setup is documented
in the README and development guide. Their historical body remains intact.

## Public export

Run from the repository root after the source and screenshots are reviewed:

```powershell
.venv\Scripts\python.exe scripts\prepare-public-repo.py --zip --init-git
```

The command creates a new directory under `.runtime/publication/<unique-id>/`,
and optionally a sibling ZIP and a fresh local `main` repository. It does not
stage, commit, push, create a GitHub repository, rewrite history, overwrite an
existing output directory or delete files. A credential-shaped match in selected
text stops export before any output is written; the error identifies only the
file and rule name. Symlinks and reparse points inside selected source paths are
rejected. A missing MIT license for the vendored runtime also stops export.

The allowlist retains application source, tests, typing stubs, scripts,
configuration examples, dependency locks, native shell source/icons, packaging
source, MIT runtime source, documentation, GitHub workflows/templates and four
reviewed screenshots. `PUBLICATION_MANIFEST.json` identifies every exported file
by size and SHA-256. No original `.git` directory is copied. The export also adds
ignore rules for future local evidence, native payloads, model weights and signing
credentials.

The manifest identifies the exported source and ZIP bytes before Git text
normalization. Git can normalize ordinary source and documentation line endings
when staging or checking out files on another platform; the manifest is not a
cross-platform checkout-byte certification. Vendored runtime source, synthetic
fixtures, deterministic goldens and the example runtime profile preserve their
bytes through Git so their embedded integrity checks remain valid.

The runtime catalog and `standard` profile in the export are newly generated,
relative-path examples with `qualified: false`. They contain no developer paths,
installed native tools, model weights, private retrieval corpus or existing
qualification receipts. Their configuration hash records only the example's
bytes. It is not approval to run a model or ship an installer. Fixture tests and
the synthetic browser demonstration do not need a live model; live inference
requires a separately configured and qualified local runtime.

## Retained fixture dependencies

| Retained path | Purpose and publication boundary |
| --- | --- |
| `fixtures/data/` named artifacts | Deterministic synthetic Sales & Margin data, governed catalog, forecasts, provenance manifest and read-only DuckDB mirror. Unknown additional data files are excluded. |
| `fixtures/powerbi/client-wow-template/` | Desktop-authored project definitions used by compiler, transaction and preparation tests. |
| `fixtures/imported/structure-twin.pbix` and `multipage.pbix` | Small scrubbed structural reports. DataModel and security bindings contain deterministic filler; images are replaced. Privacy tests inspect their text parts; these files do not contain the original employee data. |
| `evals/cases/model_contract_cases.jsonl` | Synthetic 64-case corpus used by evaluator, orchestration and preparation tests. |
| `evidence/golden-answers.jsonl` | The single evidence-directory exception: 32 deterministic numerical/forecast goldens with hashes linking them to the synthetic data above. Other evidence is excluded. |
| `evals/imported_edit/` top-level Python and JSONL files | Synthetic contexts, edit/planner golden cases and manual probe source imported by unit tests. Live probe receipts, screenshots, model logs and battery results are excluded. |
| `evals/dax_engine_differential/` source and pattern cases | Native-comparison harness and authored patterns; its source alone does not establish Desktop parity. |
| `src/ai_powerbi_analyst/llm/artifact_examples.json` | Authored examples used by the portable retrieval-relocation regression; the test creates its own temporary pinned corpus. No sibling checkout or private training corpus is required. |

The original eight-task historical retrieval test depended on a sibling
`PowerBI-RAG` configuration and private corpus. It was replaced with a portable
relocation test using authored in-repository examples. It still checks pinned
retrieval behavior across moved configurations, but no longer claims to reproduce
that historical model experiment.

The scrubbed PBIX fixtures deliberately lack a readable native DataModel. Tests
that regenerate the structural twin from the original report skip when that
private report is absent. Windows privilege-dependent symlink checks also retain
their explicit skips; neither case is silently presented as a pass. Desktop
acceptance receipts are excluded, so the source snapshot cannot imply installed
Power BI model-edit qualification.

## Validation record

The final public source snapshot was validated on October 14, 2026 with a fresh,
isolated Python 3.12.12 installation from the frozen dependency lock. The complete
backend suite passed **8,095 tests**, with **20 explicit skips**, zero failures and
zero errors in 513.53 seconds. Four workers used pytest-xdist 3.8.0 and a fresh
short temporary directory. Before/after hashes confirmed the tested source bytes
did not change during that run.

Sixteen skipped tests require original private Agents reports excluded from the
public repository. Four require unavailable Windows symlink privilege. Reviewed
synthetic fixture cases were retained and exercised rather than skipped.

| Check | Final public-source result |
| --- | --- |
| Python static checks | Ruff check and format checks passed; strict mypy passed all 199 application source files. |
| Frontend | 533 unit tests passed; ESLint, TypeScript and the Vite build passed. |
| Browser flows | Chat regressions and local-model setup at 1440/1280 pixels passed; the actual HTTP reviewer demo exercised Report, chat, Data, Model and DAX without application or console errors. |
| Dependency audits | Frozen exact-version Python audits for the application and native-tool bundle, plus npm audit, found zero known vulnerabilities. |
| Rust source shell | Formatting and locked clippy with warnings denied passed under the source-check feature. |
| Gallery | Four reviewed actual-frontend screenshots contain deterministic synthetic data and an explicit demonstration notice. |

Earlier publication tooling, portable retrieval, PBIX fixture privacy and
imported probe/contract checks passed 190 tests with two documented skips. Four
breadcrumb unit checks and edited browser-script syntax checks also passed.

Browser demonstrations use prepared synthetic responses. Live AI inference,
native Power BI edit acceptance, runtime qualification, signed installer packaging
and clean-machine installer acceptance are outside this source-validation scope.
The exported runtime remains explicitly unqualified. The portfolio prerelease
contains source code and the reviewed source ZIP.
