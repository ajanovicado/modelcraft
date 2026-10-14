# Decision and Gate Log

Every production gate is explicit. A local development test can be green while
the corresponding target-Windows acceptance gate remains pending.

## Architecture decisions

| ID | Decision | Reason | Status |
|---|---|---|---|
| ADR-001 | Power BI remains the authoritative dashboard renderer | Avoid a misleading duplicate dashboard and focus effort on the real deliverable | Accepted |
| ADR-002 | Deterministic planning emits closed executable plans; model interpretation is non-authoritative | Keeps arithmetic, paths, SQL, DAX, JSON, commands, and coordinates deterministic | Accepted |
| ADR-003 | One prepared thick PBIP plus matching DuckDB mirror | Enables offline model grounding and exact numerical Q&A | Accepted |
| ADR-004 | pbi-cli adapter with deterministic recipe boundary | Best current report-layer candidate; all behavior remains isolated and replaceable | Provisional pending Windows kill test |
| ADR-005 | Deterministic mock backend is a first-class development tool | Makes local E2E, UI, idempotency, and fault tests honest on non-Windows hosts | Accepted |
| ADR-006 | SQLite job/event journal owns active operations | Streamlit refresh must never duplicate a report write | Accepted |
| ADR-007 | Forecast and scenario are prepared, frozen, and versioned | Client-session latency and arithmetic should not depend on model reasoning | Accepted |
| ADR-008 | One reserved conversational-evidence slot | Delivers the wow loop without allowing arbitrary report mutation | Accepted |
| ADR-009 | Local validation and Desktop render verification are separate statuses | A schema-valid artifact is not proof of Desktop rendering | Accepted |
| ADR-010 | Executive-light is a frozen, hashed Power BI theme | Client polish is deterministic and validated instead of being an unverified manual step | Accepted |
| ADR-011 | Preparation and Phase 0 attestation are separate operations | Copying/hydrating a workspace cannot self-authorize real writes | Accepted |
| ADR-012 | Phase 0 acceptance is content-addressed operator attestation, not cryptographic signing | Avoids overstating the trust provided by local receipt/hash verification | Accepted |
| ADR-013 | Model interpretation is display-only; deterministic routing/planning remains authoritative | A schema-valid summary cannot expand the executable surface | Accepted |
| ADR-014 | A nonempty Desktop-generated `.pbi/cache.abf` is sealed into the reset baseline before attestation | PBIP/TMDL definitions alone do not contain imported data; a clean offline reveal must retain frozen populated model bytes | Accepted |
| ADR-015 | Three-page render proof binds a derived mutation output, not the placeholder baseline report hash | The demo baseline is intentionally one page; claiming it rendered three pages would make the gate internally contradictory | Accepted |
| ADR-016 | External validator output is untrusted; rerun local validation and content-seal the complete stage before promotion | Prevents a validator or concurrent process from changing files after the allowlist or semantic checks | Accepted |
| ADR-017 | Scripted real mode constructs no Ollama client and performs no Ollama reveal calls | The `--scripted` contract must be literal while retaining exact Desktop launch and report authority checks | Accepted |
| ADR-018 | Every answer re-hashes DuckDB, catalog, manifest, and all six governed Parquet sidecars before and after execution | Provenance cannot remain valid if any prepared driver, forecast, selection, backtest, scenario, or sales source drifts during a session | Accepted |
| ADR-019 | Startup may recover only one content-addressed pending report swap before normal verification | Crash recovery must restore a baseline-anchored report under the shared lock without weakening attestation, Desktop, reveal, path, or hash gates | Accepted |

## Phase gates

| Gate | Required evidence | Current status |
|---|---|---|
| Phase 0 — feasibility | 10/10 mutations reopen in pinned Desktop; exact rollback; model floor; strict offline | **Pending target Windows** |
| Phase 1 — data truth | DuckDB/Power BI ≤0.1%; forecast gate; exact scenario formulas; frozen claims | Prepared local artifacts and formula tests exist; independent Power BI reconciliation and Desktop evidence pending |
| Phase 2 — analyst | Exact golden answers; no invented numbers; ≤18s target ceiling | Current frozen local recomputation is 32/32 exact with zero critical fabrications; target-model latency, memory, and candidate-run evidence pending |
| Phase 3 — one-page composer | 10/10 equivalent builds; Desktop-openable; fault restoration | Local transaction/fault/idempotency coverage implemented; release-revision repetition and Desktop proof pending |
| Phase 4 — three-page composer | 10/10 build; valid IDs; grounded explain; idempotent Apply | Current local three-page artifact is accepted with zero issues by both pinned structural validators; 10-run target-Windows repetition and Desktop proof pending |
| Phase 5 — evidence loop | Exact Q&A reconciliation; reserved-slot-only replacement; reversible | Local loop and reserved-slot controls implemented; release-revision evidence and Desktop proof pending |
| Phase 6 — client UI | Durable refresh; model unload; exact Undo/reset; progress ≤5s | Current timestamped local mock-browser flow passes with a clean console; target-Windows progress timing, model unload, and Desktop reveal evidence pending |
| Phase 7 — release | Full corpus; fault/offline suite; 10 warm + 2 cold rehearsals; Desktop render | Current deterministic route baseline is 64/64 and the governed numerical recomputation is 32/32; candidate-model runs, target safety/offline evidence, rehearsals, and Desktop render remain pending |

## Gate discipline on this host

The macOS build is implementation and local verification work, not a waiver of
the Phase 0 kill gate. Real Power BI mode remains disabled until the target
Windows evidence file records exact versions, repeated mutation/reopen results,
rollback hashes, and the selected model digest.

No accepted target-Windows Phase 0 evidence package is present in this
workspace at the time of this entry. An accepted attestation verifies operator
assertions and content hashes; it is not a cryptographic signature. Historical
browser receipts and screenshots are catalogued in `evidence/README.md` and do
not certify the current revision.
