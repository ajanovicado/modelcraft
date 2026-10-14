# Security Policy

## Scope

Modelcraft is a local-first desktop application. Model inference and workspace
processing run locally. Explicit database sources connect to the selected
provider; model installation can download pinned weights when requested.

- The Python sidecar binds to loopback only and requires a per-launch bearer
  token issued by the Tauri host.
- The local Qwen model receives no shell access, raw SQL, arbitrary
  filesystem operations, PBIR JSON control, or unrestricted DAX execution.
- All SQL values are parameterized; identifiers come from the governed catalog
  only.
- Power BI file mutation is staged, hash-verified, and reversible through undo.

## Reporting a vulnerability

Please report suspected vulnerabilities privately rather than in a public
issue. Use **Report a vulnerability** on the repository's
[Security page](https://github.com/adomaticoo/modelcraft/security). Include:

- A description of the issue and affected component (e.g. sidecar API, PBIP
  build pipeline, DuckDB aggregate layer, Tauri host).
- Steps to reproduce or a proof-of-concept.
- The commit or release version you tested against.

If a fix requires coordinated disclosure, we will work with you on timing.

## Threat-model notes

The local model produces bounded proposals; deterministic code owns data access,
validation, and file mutation. Paths that bypass grounding, review, or validation
and let model output control SQL fragments, filesystem paths, unvalidated DAX,
subprocess arguments, or PBIR JSON are security issues.

Out of scope: vulnerabilities requiring physical access to the machine,
compromised local tooling (Ollama, Power BI Desktop, `pbi-cli`), or social
engineering of the user.
