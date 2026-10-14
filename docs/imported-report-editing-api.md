# Imported-report editing: HTTP and chat contract

Status: implemented; styling removed on 2026-10-02. Modules: `studio/imported_editing.py` (the service), `studio/imported_format_catalog.py`
(the Visual pane's data), `studio/imported_chat_edits.py` (chat), the `report_edit_*.py` operation families, and thin routes in
`studio/api.py` / thin methods in `studio/service.py`. This file describes what was built and was checked against the final
code (routes in `api.py`, operation classes in `report_edit_ops.py`, receipts in `imported_editing.receipt_payload`); where the
code does not do something yet it says so under "Open requests" at the end.
Design background: `imported-report-editing.md`, decisions: `imported-report-editing-review.md`,
renderer payload: `imported-report-render-contract.md`.

## Verification and limits (2026-10-02)

The earlier 40-case live-model score accepted nonempty formulas and named roles without
checking their semantics. It cannot support the earlier claim of 30/30 exact outcomes.
The corrected 2026-09-30 full local Qwen run covers 109 cases: **59/73 correct plans**,
**33/36 negative requests without plans**, and three unverified plans. Evidence:
`evals/imported_edit/evidence/planner-live-round3-all.json`. This is a development set.

Styling removal is checked separately through parser, gold-stub planner, noisy planner
and coverage modes. Stub success verifies retrieval and slot plumbing, not live-model
quality. Unverified twin checks and coverage findings remain visible in their reports.
Formula bodies are omitted from shareable evidence; local model prompts can contain DAX,
while Power Query (M) bodies remain excluded.

Prior Desktop probes, unit and UI suites, and chat-box checks are historical results.
They do not establish this revision's live behavior. Assigning a tooltip page to a host
visual and interactive filtering between synced slicers were not tested by the earlier
chat-box checks. Current verification receipts should accompany release claims.

Every error below is JSON `{code, message, detail, ...extra}`. `detail` repeats `message` (every route of the sidecar
reports its text there); a `needs_confirmation` carries its own `detail` (what would be lost). Request bodies are closed:
an unknown field is a plain FastAPI `422 {detail: [...]}`.

## Vocabulary
- **Head**: the current revision of the imported report (`report_revisions.RevisionStore.head()`), `{id: "r0007", hash: <sha256>}`.
  Every mutation names the head it was computed from (`expected_head`); a different head is a conflict (HTTP 409).
- **Batch**: one request's typed operations, applied as exactly one revision (or none). A chat message, one Visual pane edit,
  a canvas gesture (after it settles) and "Undo" are each one batch.
- **Op**: a `report_edits` operation dict with an `op` discriminator (`set_position`, `set_number_format`, `add_visual`, ...).
  Model ops (measures and the `report_edit_model` family) need Power BI Desktop and are never
  applied without an explicit confirmation from the user.

## Report payload additions (`GET /v1/workspaces/{ws}/imported-report`)
```json
{ "...existing fields...": "...",
  "head": {"id": "r0007", "hash": "<sha256>", "edited": true, "desktop_version": "2.157.1354.0"},
  "delivery": {"state": "in_sync|edited|source_changed", "source_path": "C:/.../Agents.pbix", "changes_not_in_source": 3},
  "capabilities": {"edit_report": true, "edit_model": false, "reasons": ["Power BI Desktop is not installed"], "thin_report": false,
                   "model_support": {"desktop_version": "2.157.1354.0", "documented": ["create_calculated_column", "..."],
                                     "outside": ["rename_column", "..."], "depends": ["delete_column", "set_table_properties", "delete_table"],
                                     "verified": ["rename_column", "..."], "keys": {"rename_column": "rename_column", "create_hierarchy": "hierarchies", "...": "..."},
                                     "verified_keys": ["hierarchies", "rename_column", "..."]}},
  "desktop": {"state": "closed|open|open_unsaved|dialog|unknown", "detail": "", "working_copy": "absent|placed|placed_stale|user_changes|locked"},
  "model": {"status": "current|reading|unknown|none", "...": "see The model below"} }
```
The payload is `ImportedReport` plus these five keys (`snapshot_model`, Studio's own bookkeeping, is left out). The
report's `pages`, `measures`, `tables` and `relationships` describe the head: whenever the head's model changes (a model
edit, an undo of one, an adopted Desktop save) Studio reads the model's facts back (`pbix_extract.read_model_facts` in a
bounded child process, or the copy kept for that model) and derives `tables` and `relationships` from them, so a renamed
or deleted column or table disappears from every reader. If that read fails, `model.status` is `unknown` and the joins
are read again from the head on their own (`relationships_version` 0); the local engine then refuses rather than filter
with the old model's joins. If a batch left Studio's view behind its head (a refresh that failed after the head moved),
reading the report (or a render) brings the view up to the head first, unless a batch is running (it refreshes the view
itself). A head whose refresh failed is not retried on every read: the same head is left alone for 45 s, and a batch that
starts on it tries again at once.

### The model (`model`)
What the head's model says about itself, bounded for the payload (`model_facts.payload`):

| Key | Meaning |
|---|---|
| `status` | `current`: the sections below describe the head. `reading`: Studio is reading them (a report imported before facts were kept; read once in the background, the payload says `reading` meanwhile). `unknown`: they could not be read, or the read said nothing of the model's tables and columns (then no calculation group or field parameter can be ruled out, and readers must not assume there are none). `none`: the report has no model of its own (a thin report). Only `status` is present unless it is `current`. |
| `version` | 1 |
| `truncated` | The sections that lost text or items (absent when nothing was cut). An item that lost text says `"truncated": true`. Power Query text, role filters and format expressions are cut at 4,000 characters here, calculated DAX at 20,000; measure DAX is left out (the payload's `measures` rows carry it). The full facts are the `GET .../imported-report/model` route's. |
| `model` | `{culture, sourceQueryCulture, discourageImplicitMeasures, defaultMode, autoDateTime}` |
| `tables` | `[{name, hidden, private, showAsVariationsOnly, autoDateTable ("local"\|"template"\|null), description, dataCategory, isDateTable, dateColumn, source, calculated, expression, calculationGroup, lineageTag}]` |
| `columns` | `[{table, name, kind ("data"\|"calculated"\|"calculatedTableColumn"), dataType, dataTypeName, dateTimeType ("date"\|"dateTime"\|"time"\|null), state, expression, formatString, sortByColumn, dataCategory, summarizeBy, summarizeByCode, hidden, key, displayFolder, description, sourceColumn, lineageTag, error}]` |
| `measures` | `[{table, name, formatString, formatStringExpression, displayFolder, description, hidden, dataType, dataTypeName, lineageTag, state, error}]` |
| `relationships` | `[{name, fromTable, fromColumn, toTable, toColumn, fromCardinality, toCardinality, cardinality, crossFilter, active, relyOnReferentialIntegrity, securityFilter, joinOnDate}]` |
| `hierarchies`, `variations`, `roles`, `calculationGroups`, `fieldParameters`, `whatIfParameters`, `partitions`, `expressions` | As `pbix_extract.read_model_facts` documents them (roles carry their table filters, never their members). |
| `studio.rowLevelSecurity` | A sentence when the model has roles: Studio's canvas and answers use every row (as Desktop does unless you view as a role). |
| `studio.autoDateTime` | The model's auto date/time setting. |
| `studio.powerBiOnly` | `[{table, kind: "calculationGroup"\|"fieldParameter", detail}]`: visuals that use them are drawn by Power BI only. |
| `studio.changedCalculations` | `[{table, column}]` (`column` null for a table): calculated objects the imported rows do not reflect. Studio's engine calculates them when it can do so exactly and otherwise refuses questions that read them. With the rows' own model unknown every calculated object is listed. |
| `studio.data` | `{known, stale, tables, detail}`: whether Power BI Desktop refreshed or added data tables since Studio read the rows. |
| `studio.actions` | `["reread_data"]` when reading the data again helps (the data is stale, or the rows' own model is unknown), else `[]`. |

Two routes serve the model beyond the payload:
- `POST /v1/workspaces/{ws}/imported-report/reread-data` `{idempotency_key}`: decodes the head's rows again into a new
  snapshot that becomes the workspace's (`StudioService.reread_imported_data`), on the report's lane and under its edit
  lock; answers `200 {snapshot_id, model}` or `202` while it runs (poll the edit status route). `404 no_model` for a thin
  report.
- `GET /v1/workspaces/{ws}/imported-report/model?table=<name>`: the head's full facts (Power Query and measure DAX
  uncut) for the model workbench, or only what belongs to one table (its entry, columns, measures, partitions,
  hierarchies, variations, calculation group or parameter, and the relationships and role filters that touch it).
  `truncated` still lists any section the read itself had to cut (texts over 1,000,000 characters, or a model over
  12,000,000 characters of facts): the workbench must not edit an item that says `"truncated": true`. `404
  unknown_table`, `404 no_model`, `503 model_unreadable`. Formula and query bodies are for local display, editing and
  checks only. Nothing is sent to an external service: prompts go only to the local model. They may carry the
  report's measure definitions and DAX the user typed, but never Power Query (M) text; M typed into chat is replaced
  where it stands and the rest of the message is kept (`tests/unit/test_model_facts_privacy.py`). Shareable eval
  evidence records formula hashes, not bodies.
- `POST /v1/workspaces/{ws}/imported-report/model/validate-query` `{expression, table?}` (`ModelQueryCheckRequest`:
  `expression` 1-50,000 characters, `table` 1-200, nothing else): what an edit with this Power Query formula would be
  told, without queueing one or touching Desktop. With `table` it is checked as `set_table_query` would check it (the
  table's own checks first: it must exist and load from a single Power Query query, else the refusal and no parser run),
  without it as `set_named_expression`'s formula. Answers `200`:
  `{valid: bool, checked: bool, message: str | null, problems: [str], warnings: [str], table: str | null}`.
  `message` is the refusal the edit would get, in the model family's words (for example "The Power Query formula is not
  valid: Expected 'in'", listing at most three problems; "The query cannot contain control characters"; "'Years' is
  calculated with DAX, so it has no Power Query query"; "The model has no table 'X'"), null when it would go ahead.
  `problems` is everything Microsoft's Power Query parser found (`llm/m_validation.cjs` under the bundled node,
  `report_edit_model.check_m`). `checked` is false when that parser could not run; then `valid` only means nothing refused
  it and `warnings` says Power BI checks it when it loads the data. `table` is the table's own spelling. The table checks
  use the model facts Studio already has (never a new read); without them they are left to Power BI Desktop, as in an
  edit, and a warning says so. The formula is never logged, stored or sent anywhere but the local parser. `404 no_model`
  for a thin report; `422` for a body that does not fit.

- `pages[].page_type` is `default`, `drillthrough` or `tooltip` (the page's `type`, else its `pageBinding.type`, re-read
  after every edit, so `set_drillthrough`, `set_tooltip_page` and `clear_page_type` show at once).
  `pages[].visuals[].alt_text` is the visual's alt text (`visualContainerObjects.general` `altText`, decoded), null when
  it has none: what the Format pane's alt text editor starts from (the render's formatting tokens do not carry it).
- `head.edited` is false while the head has the imported file's content (a fresh import, or after restoring r0000).
  `head.desktop_version` is the Desktop version recorded with the head row (a model edit or a Desktop save), else null.
- `capabilities.edit_report` is false for a legacy-layout report (`format != "PBIR"`); its reasons say how to upgrade it.
  `capabilities.edit_model` is false unless Desktop, the bridge and `pbi` are pinned, the installed Desktop version has a
  passing contract receipt, and the report has a model. `thin_report` reports (no DataModel) show a persistent notice that
  report edits cannot be verified locally. While Studio is still looking for Desktop (the first look after start takes
  seconds and never blocks this call) `capabilities.checking` is true and `edit_model` is false; ask again shortly.
  Contract receipts are read from `<state>/desktop-receipts/desktop-<version>.json` first, then from
  `evals/imported_edit/evidence` (in the packaged app, from `sys._MEIPASS`). The build script bundles that folder when it
  exists; until a contract check has been run and its receipts committed there, a packaged Studio has no receipt and keeps
  `edit_model` false. `capabilities` is the readiness surface of imported editing (`preflight.Readiness` is not extended).
- `capabilities.model_support` says which `model` family operations the installed Desktop may be sent
  (`imported_editing.model_support_payload`), from the same source the editor gates them with: the `verified_ops` of the
  model probe receipt for exactly the installed version (`model-probe-<version>.json`, read by
  `DesktopController.model_probe_coverage`; one file read, Desktop is never started).
  - `desktop_version`: the installed version, null without Desktop or while Studio is still looking for it.
  - `documented`, `outside`, `depends`: the operations in Microsoft's documented list for external tools, those outside
    it, and those that depend on the object (`delete_column`, `delete_table`, `set_table_properties`: documented on a
    calculated column or table, outside on one loaded from the data source) (`report_edit_model.MICROSOFT_SUPPORT`).
  - `keys`: each `outside` and `depends` operation's probe key (`report_edit_model.support_keys`; several operations share
    one: the hierarchy operations are `hierarchies`).
  - `verified_keys`: the probe keys the receipt lists (unknown keys dropped); `verified`: the `outside` and `depends`
    operations whose key is among them.
  An `outside` operation not in `verified` is refused before Desktop opens (the refusal gives Desktop's steps), so the UI
  disables it. A `depends` one not in `verified` still works on a calculated object and is refused on a data-source one.
  The measure operations are not listed: they are always documented.
- `desktop.state` only looks (never launches or touches Desktop) and is cached for 3 s and dropped when a batch finishes.
  `dialog` names the dialog in `detail`; `unknown` means Studio could not tell (`detail` says why), which includes the first
  seconds after start while the search for Desktop is still running (`detail` "Still looking for Power BI Desktop"; that
  answer is not cached). A lookup that found nothing is repeated after 30 s and a found Desktop is re-checked after 5 min, by
  a background search: a read returns the last answer at once and the next one sees the new one.
  `working_copy` is how Desktop's copy relates to the history (`report_revisions.WorkingCopyState`), `locked` when
  Windows will not let Studio read it.
- `delivery` follows `report_delivery`'s rules; the source file's hash is remembered by (path, size, mtime) so a burst of
  reads hashes it once.

## Mutations
All mutations require `idempotency_key` (8-200 chars) and `expected_head`. Long batches (anything that touches Desktop, up to
~90 s) run on a **per-report serial worker that is not tied to the HTTP request** (`ImportedEditingService`, one lane per
report: batches of one report run one after another, different reports side by side); the response is returned when the batch
finishes, or as `202 {"batch_id", "state", "kind"}` after 25 s, after which the client polls
`GET .../edits/{batch_id}`. The batch keeps running when the client goes away (a closed chat stream, a reload).
Cancel is honoured only while the batch is `queued`.

Idempotency: the same key with the same request returns the same batch (its recorded outcome once finished, the running
batch while it runs) and is **not** judged against the head again (the first attempt may have moved it); the same key with a
different request (operations, `expected_head`, `summary`, revision or destination) is `409 idempotency_conflict`. The consent
flags (`desktop_work`, `confirm_replace`, `confirm_model`, `confirmed_discard`) are not part of the request's identity, so
"Desktop has unsaved work" is answered by retrying the same edit with `desktop_work: "save"` under the same key: a batch that
failed may be asked for again under its key. After a restart the edit journal answers a repeated key (`replayed: true`).

`answers` are the user's replies to a `needs_choice` question: each `{step, fill}` (1-based step, a non-empty `fill` object of at
most 20 fields) is written over the fields of that operation before anything is parsed, so a choice such as
`{"overrides": "clear"}` or `{"fields": [...]}` is applied as if the operation had been sent whole. A fill may not change
`op`; a bad step or an empty fill is a `422`. The merged operations are what the idempotency fingerprint sees.
Batches are mirrored in `reports/<id>/batches.jsonl`, so a poll for an old batch still answers after a restart: `done` with
its receipt, or `failed` with code `interrupted` when the process died inside it (recovery has then finished or undone it).

| Method and path | Body | Success |
|---|---|---|
| `POST /v1/workspaces/{ws}/imported-report/edits` | `{operations: [op...], expected_head, idempotency_key, summary?, desktop_work?: "ask"\|"save", confirm_replace?: bool, confirm_model?: bool, answers?: [{step, fill}], confirmed?: ["report_tests_broken"]}` | `200 EditReceipt` |
| `GET  /v1/workspaces/{ws}/imported-report/edits/{batch_id}` | - | `{batch_id, state: "queued\|running\|done\|failed", kind, stage?, label?, receipt?, error?}` (`stage`/`label` are the latest progress step, absent while queued; 404 for a batch of another report or an unknown id) |
| `POST /v1/workspaces/{ws}/imported-report/edits/{batch_id}/cancel` | `{}` | `{batch_id, state, kind, cancelled: bool, error?}` (`cancelled` only while it was queued; then `state: failed`, `error.code: cancelled`) |
| `POST /v1/workspaces/{ws}/imported-report/undo` | `{expected_head, idempotency_key, desktop_work?}` | `200 EditReceipt` (newest change only) |
| `GET  /v1/workspaces/{ws}/imported-report/history` | - | `[{id, parent, author: "import\|studio\|desktop\|undo\|restore", summary, created, size, available, is_head, is_placed, can_undo, conflict, desktop_version, restores?, operations: [{op, summary}]}]` newest first |
| `GET  /v1/workspaces/{ws}/imported-report/history/{rev}/discard-preview` | - | `{would_discard: [history row...]}` (later changes a restore would leave out) |
| `POST /v1/workspaces/{ws}/imported-report/restore` | `{revision_id, expected_head, idempotency_key, confirmed_discard: bool, desktop_work?}` | `200 EditReceipt` (`409 needs_confirmation` `restore_discard` with `would_discard` until `confirmed_discard`) |
| `POST /v1/workspaces/{ws}/imported-report/save-copy` | `{destination, idempotency_key}` | `200 {path, hash}` |
| `POST /v1/workspaces/{ws}/imported-report/replace-original` | `{idempotency_key, confirm: true}` | `200 {path, hash, backup, warnings}` |
| `POST /v1/workspaces/{ws}/open-source-report` | `{}` | `200 {launch_requested, already_open}`, or `202 {batch_id, state, kind, launch_requested: true, already_open: false}` while Desktop is still starting |
| `GET  /v1/workspaces/{ws}/imported-report/format-catalog?visual_type=` | - | the Format pane's contents (below); `404` for a visual type the catalog does not have |

`save-copy` takes an absolute `.pbix` path or an existing folder (then `<name> (edited).pbix`). It is created exclusively, so a
taken name gets ` (2)`, ` (3)` and nothing is overwritten. Refused with `422 edit_rejected` (plain words): a path that is not
`.pbix`, a relative path, a folder that does not exist, anything inside Studio's own state folder, and a name that is a link.
It first records a save the user made in Desktop, refuses while Desktop shows unsaved changes, and checks the head before and the
file after writing. `replace-original` needs `confirm: true` (a body without it is a `422`), refuses when the user's own file
changed after the import (or after Studio last wrote it) or Desktop has it open, makes `<name>.pbix.studio-backup-<UTC time>`
first, and swaps the file in atomically. Both run on the report's lane, so they wait for a batch in progress.
`open-source-report` adopts a Desktop save into the history, puts the head into Desktop's working copy and opens it; it
unloads the local model first only when it actually launches Desktop. Its `202` body is the old route's answer (the launch is
under way) plus the batch to poll.

`EditReceipt`:
```json
{ "revision": {"id": "r0008", "parent": "r0007", "author": "studio", "summary": "...", "created": "..."},
  "head": {"id": "r0008", "hash": "..."},
  "summaries": ["Made the 'MTD Sales by Group' bars green."],
  "warnings": ["..."],
  "adopted": null,
  "protected": [{"visual": "e82e02f9...", "what": "conditional font color on Agent ID"}],
  "replaced": ["TopN blue, BottomN red"],
  "visible_in": [{"op": "set_number_format", "where": "studio|powerbi_only|covered_by_background", "detail": "..."}],
  "pending_model_operations": [{"op": "update_measure", "name": "Status Color", "expression": "..."}],
  "outcome": "updated|partial|failed",
  "desktop": "closed|reopened|open",
  "replayed": false, "batch_id": "<the id to poll>", "journal_batch": "<the edit journal's id for it>",
  "review": [ReviewFinding],
  "model_health": {"new": [HealthFinding]},
  "tests": {"results": [TestResult], "broken": [TestResult], "text": "...", "text_broken": "..."} }
```
What Studio noticed about an `edit` (never an undo or restore); none of it blocks or fails a committed edit (any error in
these checks gives `[]` / leaves the field out):
- `review` (always on an edit): the page findings the edit introduced or made worse (`report_review.review_edit` of the
  touched pages before and after; the touched visuals first; a page the edit added has only new findings; at most 12 pages
  and 20 findings). Old issues are never repeated; "review this page" lists them. Shape: `Finding` of
  docs/imported-report-review.md.
- `model_health` (only when the batch changed the model): `new` = the model findings (docs/imported-model-health.md) that
  name an object the batch touched and that the model did not have before, or that now name a touched object they did
  not name before. Messages name objects only (no formula).
- `tests` (only when the batch changed the model and the report has tests): the tests' results after the edit (recorded as
  the new head's run), the ones the user let it break (`confirmed: ["report_tests_broken"]`), and the plain texts;
  `text_broken` is also added to `warnings`. A rename in the batch is followed by the stored tests (ids kept).
`adopted` is the revision row (same shape as `revision`) of a Desktop save that was recorded first, else null. `outcome` is
`partial` when the change is recorded but something after it (the working copy, Studio's refreshed view, the activity log)
did not complete; the `warnings` say what.

What the edit layer reports beyond its one-line summaries (`report_edit_context.HandlerResult`, carried by
`report_editor.EditReceipt` and summed into `EditOutcome`):
- `protected` `[{visual, what}]`: formatting the edit left alone on purpose (conditional formatting the user did not agree to
  replace, a visual a theme change does not reach). The receipt says so, so "done" never hides "not touched".
- `replaced` `[str]`: what a whole-visual write overwrote in narrower entries ("TopN blue, BottomN red"). It also appears in
  the summary text, and the HTTP receipt carries it as its own field.
- `visible_in` `[{op, where, detail?}]`: `studio` when the canvas draws the change, `powerbi_only` when only Power BI shows it
  (a property or visual type Studio does not draw: "check it in Power BI"), `covered_by_background` when an opaque page picture
  hides it. `op` is the operation name; the page background and shape operations add a `detail` sentence.
- `pending_model_operations` `[op]`: measure changes the edit layer only *suggests* (a color hard-coded inside a measure's DAX
  when a recolor was asked). They are shown to the user and never applied. Both `EditReceipt` and the HTTP body carry them
  (as operation objects, in the same shape as the request's `operations`).

`protected`, `replaced`, `visible_in` and `pending_model_operations` in the HTTP body come from the editor's receipt, also when
the receipt is the replay of a recorded batch (what the edit layer reported is recorded with the batch; a batch recorded before
that has none of it). Only what the receipt lacks is filled in another way: `protected` and `visible_in` are worked out again
by applying the same operations to the revision the edit was made on (a pure, in-memory run: the same answer the edit had). If
that re-run cannot be done (the parent revision was pruned), `protected` is `[]` and `visible_in` is worked out per operation:
`studio` for layout and structure operations and for a property the canvas draws, `powerbi_only` for a property Studio has no
token for or a visual type Studio only frames (gauge, treemap, ...). A receipt with no `visible_in` (an undo, a restore) gets
the per-operation reading too.
Error shapes (JSON `{code, message, detail, ...}`):
- `409 head_moved` `{expected, actual}` - another edit or a Desktop save came first; the UI refreshes and offers to redo.
  Raised before anything is queued when the caller's head is already stale, and again by the edit layer under the lock.
- `409 desktop_unsaved` `{choices: ["save_and_continue","cancel"]}` - Power BI Desktop has unsaved changes to this report;
  never saved silently. (A preview refuses the same way with `choices: ["cancel"]`.)
- `409 desktop_dialog` `{dialog}` - a dialog is open in Desktop; the user must finish it.
- `409 desktop_busy` - Desktop has the working copy open, so Studio cannot replace it.
- `409 desktop_stale` - a preview was asked while Desktop shows an older version than the head.
- `409 edit_busy` - another change to this report is still being saved.
- `409 needs_confirmation` `{kind: "model_edit"|"restore_discard"|"replace_conditional"|"replace_dynamic"|"remove_page_references"|"report_tests_broken"|"model_health_fix", detail}`
  (`replace_*` and `remove_page_references` are the edit layer's; answers are in the table below);
  `restore_discard` adds `would_discard: [{id, parent, author, summary, created}]`; `report_tests_broken` adds
  `tests: [TestResult]` (the tests the change would break) and `confirm: {confirmed: ["report_tests_broken"]}` (what to send
  back); `model_health_fix` adds `finding`, `outside_supported`, `needs_desktop`.
- `409 needs_choice` `{question, choices: [{index, label, fill?}]}` - the request has several readings (the edit layer asks).
- `409 idempotency_conflict`, `409 conflict` (any other clash: a write refused by Windows, a stale stored row),
  `409 cancelled`, `409 interrupted` (only inside a polled batch).
- `422 edit_rejected` `{message, step}` - the ops cannot be applied (plain English, names the offending step; `step` is null
  when the problem is not one step's); also a `save-copy` destination Studio may not write, and an undo or restore with nothing to go to.
- `422 edit_unsupported` `{capability, reason, desktop_steps}` - an operation Studio does not have (`capability` is its name,
  or `unknown` when the step names none), and a legacy-layout report (`capability: "legacy_layout"`, with the steps).
- `422 needs_desktop` `{needs_desktop: true}` - from the render routes only: Studio's compiler cannot draw the page (Desktop
  would refuse it too); offer "Preview in Power BI".
- `503 desktop_unavailable` `{reasons?}` - Power BI Desktop / bridge / `pbi` missing or not yet checked, for model edits.
- `500 edit_failed` - an unexpected failure; the message is generic and the detail is in the sidecar log.

A model operation (the measure operations and every `model` family operation) is refused before it is queued:
`503 desktop_unavailable` when `capabilities.edit_model` is false (the reasons are in the body; for a `model` family
operation also when Studio's model tool cannot run), else `409 needs_confirmation` `{kind: "model_edit", detail}` until
the request carries `confirm_model: true`. The message says "Changing a measure ..." for measure operations only, else
"Changing the model ...". `detail` is one sentence per model operation, worded from the operation itself
(`imported_editing._model_line`): "Create the measure X in T.", "Hide DimStore[StoreKey] in the model.", "Rename the
table DimChannel to Channel.", "Create the relationship FactSales[StoreKey] → DimStore[StoreKey].", "Format Date[Month]
as “mmm”, and sort it by Month Number."; it never quotes a formula or a query.

**The edit layer's own questions are answered inside the operations, not by a request flag.** A `needs_confirmation`
(`replace_conditional`, `remove_page_references`) is answered by sending the same batch again with the
matching per-operation flag set (table below), and a `needs_choice` by merging the chosen option's `fill` into the operation it
is about. The request-level `confirm_replace: true` sets `replace_conditional` and `replace_dynamic` to `true` on every
operation that declares them (it never sets `keep_conditional`); operations without those fields are unchanged. The Studio UI
and chat set the per-operation flags themselves.

| `needs_confirmation.kind` | The question | Answer: set on the operation |
|---|---|---|
| `replace_conditional` | A conditional-format or number-format write would replace existing rule-driven formatting | `replace_conditional: true` on an operation that declares it |
| `remove_page_references` | Buttons or bookmarks still point at the page being deleted | `cleanup_references: true` on `delete_page` |
| `model_edit`, `restore_discard` | see above and the routes table | `confirm_model: true` / `confirmed_discard: true` on the request |
| `report_tests_broken` | A model or measure change would change numbers pinned as report tests (passed before, fail after). Asked before Power BI Desktop is opened when the batch's measures decide it, else after Desktop saved and before the revision is committed (the head stays) | `confirmed: ["report_tests_broken"]` on the request (the whole batch, not an operation) |
| `model_health_fix` | Applying a model health fix (always asked) | `confirmed: true` on `POST .../model-health/fix` |

| `needs_choice` | Options (`fill`) |
|---|---|
| `add_visual`, `set_fields` when a numeric column needs an aggregation | `{fields: [<the field list with "aggregation" set>]}` |

## Judgement: review, model health, report tests, explain
Deterministic checks of the report (docs/imported-report-review.md, imported-model-health.md, imported-report-tests.md,
imported-number-explain.md). No finding, number or test result comes from the language model. Errors use the envelope above.

| Method and path | Body / query | Success |
|---|---|---|
| `POST /v1/workspaces/{ws}/imported-report/pages/{page}/review?revision=` | `{with_data?: true}` | `Review.to_dict()` + `head {id, hash}`. `page` is an id, a display name or a 1-based position. `with_data` draws the page with Studio's engine first (never Desktop); a page the engine cannot draw is reviewed from the document alone (`skipped` says which rules). `409 head_moved` for another `revision`, `422 unknown_page`, `422 report_unreadable`. Fixes are applied with the `edits` route (one revision, undoable). |
| `GET  /v1/workspaces/{ws}/imported-report/model-health?stats=0\|1` | - | `{known, findings, summary{problems, suggestions, by_category, by_rule}, checked{tables, columns, measures, relationships}, notes, head{id, hash}}`. From the head's **full** model facts; unknown facts give `known: false` (never an error). `stats=1` adds column statistics from the rows Studio decoded, unless Power BI refreshed the data since. Kept per (revision, model, rows, stats). |
| `POST /v1/workspaces/{ws}/imported-report/model-health/fix` | `{finding_id, base_hash, confirmed?: false, idempotency_key, desktop_work?}` | the `EditReceipt` of the fix (one batch through the normal pipeline: model gate, Desktop, report-tests gate), or `202` while it runs. `409 needs_confirmation` `model_health_fix` without `confirmed` (`detail` = the fix summary); `409 head_moved` when `base_hash` is not the head's hash; `409 finding_gone` ("This was already fixed or changed"); `422 no_fix` for a finding whose fix is a judgement call. The same `idempotency_key` gives the batch that ran. |
| `GET  /v1/imported-report/model-health/rules` | - | `[{id, category, severity, title}]` (26 rules) |
| `GET  /v1/workspaces/{ws}/imported-report/tests` | - | `{tests: [TestDefinition], last_run: TestRun\|null, stale}` (`stale`: the last run was for another head) |
| `POST /v1/workspaces/{ws}/imported-report/tests` | `{name, measure?: {table, name}, expression?, filters: [TestFilter], expect, format?}` | `TestDefinition` (`422 invalid_test` with the first problem) |
| `POST /v1/workspaces/{ws}/imported-report/tests/pin` | `{page_id, visual_id, point: {query_ref: value}, value, measure?, name?}` | `{test, result}`: pinned with the canvas's own filter context, checked on Studio's engine and kept whatever the result (so the UI can say "Studio calculates 850 here; the card showed 851"). `422 pin_refused` with the reason. |
| `PATCH /v1/workspaces/{ws}/imported-report/tests/{test_id}` | `{name?, expect?, filters?}` | `TestDefinition` (the id stays); `404 unknown_test` |
| `DELETE /v1/workspaces/{ws}/imported-report/tests/{test_id}` | - | `{removed: bool}` |
| `POST /v1/workspaces/{ws}/imported-report/tests/run` | `{engine?: "local"\|"desktop"\|"auto"}` | `TestRun` + `text` (`describe_results`), recorded for the head. `auto` asks Power BI Desktop for the tests Studio's engine left unchecked, only when Desktop already has exactly the head open and saved (never opened for this); `desktop` without that checks nothing. |
| `GET  /v1/workspaces/{ws}/imported-report/tests/suggestions?page=<id>` | `page` repeatable | `{tests: [TestDefinition], text: [sentence]}`: proposed from the pages Studio's engine draws, never stored |
| `POST /v1/workspaces/{ws}/imported-report/explain` | `{page_id, visual_id, point?: {values: {query_ref: value}, measure?}, canvas?: LiveReportRequest, focus?: "value"\|"colour"\|"blank"}` | `Explanation.to_dict()` (docs/imported-number-explain.md; `formula` is for the Explain panel only). Studio's engine, else Power BI Desktop's as the live report chooses. `404 unknown_visual`, `422 invalid_point`, `422 invalid_canvas`, `422 cannot_explain` (the page cannot be read or compiled), `422 needs_desktop` (no rows and no Desktop). |

The CORS allow-list includes `PATCH` and `DELETE` for the tests routes.

## Render payload
Routes (all `POST`, body `LiveReportRequest` `{selections, searches}` for the first two):
- `/v1/workspaces/{ws}/imported-report/pages/{page_id}/render?revision=r0007` draws the page with data. `revision` (the head id the client believes in) is a
  **query parameter, not a body field** (the body is closed and rejects it). A non-head revision is
  `409 head_moved {expected, actual}`, and the payload echoes `revision` and `head_hash` (see the render contract). Without
  `revision` there is no check and the payload's `revision` is null.
- `/v1/workspaces/{ws}/imported-report/pages/{page_id}/render-formatting?revision=r0007&only=<visual id>&only=...` re-reads how visuals look after an edit that
  changed no data: no DAX, no engine, no Desktop, and the same head check. `only` is a repeatable **query parameter** and is
  optional. Studio's own canvas leaves it out and re-reads the whole page (no DAX, so it costs the same, and a partial answer
  would leave the ranks, positions and hidden flags of the visuals it did not name stale).
- Both answer `422 needs_desktop` when the compiler cannot read the page (a slicer's saved selection, for example: every visual
  would be drawn unfiltered), and the old "Studio's engine declined, ask Desktop" fallback of `render` is unchanged for engine
  errors.
- `/v1/workspaces/{ws}/imported-report/pages/{page_id}/preview?revision=r0007&refresh=false` takes a Power BI Desktop screenshot of the head: an older
`revision` is `409 head_moved`; a Desktop with unsaved changes is `409 desktop_unsaved`; Desktop showing an older version
than the head is `409 desktop_stale`; a picture already kept for this head is served without asking Desktop.

## Visual catalog (`GET .../format-catalog?visual_type=clusteredColumnChart`)
`{visual_type, family, studio_draws, instance_selector, sections, all_properties}`:
- `sections` is the curated list in pane order, each `{id, title, controls}`. Only retained edits are exposed:
  size and position, number formats, conditional formatting and structural table or matrix layout.
  Styling controls and styling search results are omitted. Existing formatting remains part of the render payload.
- A control is `{id ("object.property"), section, label, description, kind: "toggle|number|color|select|text|font", object,
  property, where: "objects|visualContainerObjects|position", type (the catalog's), writable, options: [{value, label}]?,
  unit?, min?, max?, studio_render, instance_selector: {"id": "default"}?, theme_default, op, generic_op}`.
  `op` is `{name, field, fixed}`: the operation that writes it, the argument that takes the value and the arguments it needs
  besides (`{op: name, visual, **fixed, field: value}`); `generic_op` is null because generic styling writes are removed.
  Select values use the operation's own vocabulary, such as `"thousands"` for number-format display units.
  A control no retained operation can write correctly is left out of `sections`, and its `all_properties` row says
  `writable: false`. `studio_render: false` means the canvas does not
  draw the property (say "visible in Power BI only"). `theme_default` is what the theme gives the property when the visual sets
  nothing (the "Auto (theme)" placeholder), null when it says nothing. `writable: false` properties (dash arrays, style
  presets, field-bound values) are listed but cannot be edited.
- `studio_draws` is false for a type Studio only frames (gauge, treemap, ...); `instance_selector` is
  `{id: "default", states: [...]}` for types whose formatting only takes effect under a state selector (shape, cardVisual,
  actionButton, pageNavigator, bookmarkNavigator, filterCard, pivotTable); the edit operations add the selector themselves.
- `all_properties` is every property of the type, sorted, for the search box: `{object, property, where, label, description,
  kind, type, subtype, writable, options, studio_render, instance_selector, curated}`.

## Recovery and Desktop discovery
At start-up (after the app is serving, in a background thread) the service finishes or undoes the batches a crash left in each
report's edit journal (`ReportEditor.recover`), removes orphan staging files, brings a view that fell behind its head up to
date, and remembers which Desktop instance has each working copy open (never closing or touching it); each finding is logged
to the workspace's event log as `report.edit_recovered`. A report that was never edited or opened costs nothing. Every batch's
outcome is logged as `report.edit_applied` or `report.edit_failed`, so a reload can show what a closed chat stream started.
Power BI Desktop is looked for on first use (pinned bridge and `pbi`, the installed version, a passing contract receipt) and
again after 30 s while it is missing or 5 min once found. Without it a report-only edit works exactly as with Desktop closed.

## Chat (`POST /v1/chat/stream`, SSE)
For an imported report the message is read by `edit_intents.parse_edit_message` (no model) and carried out by
`imported_chat_edits.imported_edit_events`, called from the imported branch of `StudioService.chat_events`. A message that only
asks (no edit clause) is answered exactly as before. New events, in addition to `token`, `heartbeat`, `draft`,
`report_action`, `done`, `error`:
- `edit_progress` `{stage: "planning|closing_desktop|writing|opening_desktop|applying|saving", label}`. Also
  `planning` "Asking the local AI to plan your request (up to a minute)" before the planner's model call, and "Asking
  the local AI to write the formula (up to a minute)" before a formula draft (see "Planner" below).
- `edit_plan` `{steps: [{summary, op, needs_desktop, confirm, source: "cards|model|parser", outside_supported, verified_on?}],
  ops: [...], detail, needs_desktop,
  left_out: [text...], what_if?: {question, answer}, pending_id}` - what the planner made of text the parser could not
  read, **applied only on Apply**: `POST .../chat/edit-confirm {pending_id, accept: true}` applies every step as ONE
  revision (Undo takes the whole plan back), `accept: false` leaves the report alone ("Okay, I left the report as it
  was."). `steps` are in execution order, one plain line each, worded from what the step does ("Remove fields: ...",
  "Hide a page: page 'Forecast'", a visual with its page when the report has several); `confirm` marks a card that is
  never applied without a yes (Apply is that yes, and `detail` says so); `ops` are the operations Apply sends
  (already accepted by `parse_operations`); `detail` is the text of the plan card (the steps; notes such as "Studio
  reads the formula as: ...", notes about values omitted because the request did not give them; the local what-if of a
  measure change; the Desktop note worded from what the model change changes, a measure, columns, relationships...;
  and, when report steps and a Desktop step go together, that none is applied if Desktop cannot make the model change).
  `needs_desktop` is true when a step is a model operation: the Apply is then also its confirmation (the port is called
  with `confirm_model=true`). `what_if` (a plan that changes a measure's formula) is the local what-if answer, as for a
  measure change the parser read (P32). `left_out` are parts of the message past the four steps one plan holds (the
  reply names them). Recorded with the turn like the other `edit_*` events. `outside_supported` is true for a model step
  outside Microsoft's documented list of changes external tools may make (decided as the editor's gate decides it,
  `ImportedEditingService.model_step_support`: a delete of a calculated column is documented, of a column loaded from the
  source it is not; an object an earlier step creates counts as calculated); such a step also has `verified_on`: the
  installed Desktop version when its model probe receipt covers the step, else null (Apply would be refused with Desktop's
  steps, so the card can say so up front). The same source as `capabilities.model_support`.
  The desktop app draws `edit_plan` with Apply and Cancel. For client compatibility, the same plan also comes as
  `edit_confirm` with `kind: "plan"` (same `pending_id`, `ops`, `detail`, `steps`, `what_if`). The app skips that
  duplicate confirmation by `pending_id`; either event answers the same pending question. The reply says
  "Choose Apply to report ... or Cancel" (the button the app shows).
- **A typed yes or no answers the open confirmation.** While the chat's last question is an
  `edit_confirm` (a model change, a plan) that has not expired, a short message that is clearly yes
  ("yes", "yes, go ahead", "go ahead", "do it", "apply", "ok", "confirm") or no ("no", "cancel",
  "don't", "stop") on `POST .../chat` answers it exactly as its Apply or Cancel would (the history
  shows the words typed). Anything longer or not clearly yes or no is a new message, and a new
  message ends that: a later "yes" no longer answers the question before it (its buttons still do).
  A question with choices (`edit_choices`) is never answered by a typed yes.
- `edit_applied` `{revision_id, parent_id, head: {id, hash}, kind: "edit|undo|restore", ops: [...], summaries: [...],
  warnings, visible_in, protected, can_undo: true, outcome, adopted, desktop, replayed, targets: [label...],
  clauses: [{text, status: "done|asked|unsupported|needs_confirmation", summary}], receipt: <the EditReceipt>}`.
  `summaries` are what the edit layer says it wrote (they name the target and what was replaced); `targets` are the visuals
  the clauses resolved to; `clauses` is the per-clause receipt of the whole message.
- `edit_choices` `{question, choices: [{index, label}], pending_id}` - answered by `POST .../chat/edit-choice`. Sent for a
  CLARIFY of the parser (which visual? which page?), for the suggestion chips of the fallback tier, for a choice the edit layer
  asks (`needs_choice`, for example "clear or keep the overrides") and after a measure edit (update the captions that still say
  the old number, rename the measures that do).
- `edit_confirm` `{kind, what_if?: {question, answer}, ops: [...], detail, pending_id, would_discard?}` - answered by
  `POST .../chat/edit-confirm`. `kind` is `model_edit` (a measure or other model change: **never applied automatically**;
  `what_if`, when present for a measure change, is the local answer computed first; `detail` says it needs Power BI Desktop,
  about 1 minute; it does not promise a model
  unload, because only "Open in Power BI" calls `before_launch`), `restore` (going back over later changes;
  `would_discard` lists them), `desktop_unsaved` (Desktop has unsaved work; yes saves it into the history first, never
  silently), or the edit layer's own kind (`replace_conditional`, `replace_dynamic`, `remove_page_references`: yes sets the
  matching flag on the operations).
- `edit_failed` `{message, code}` - the plain message is also the text reply. `code` is the API's error code
  (`head_moved`, `desktop_dialog`, `edit_busy`, `edit_rejected`, `edit_unsupported`, `desktop_unavailable`, ...), `too_many`
  (more than 50 operations in one message: nothing is applied) or `expired` (an answer to a question that is gone).
- `edit_review` `{revision_id, findings: [ReviewFinding]}` - right after an `edit_applied` whose receipt has `review`
  findings (what the edit introduced on the page; "Studio noticed" in the UI, each with its fix to Apply).
- `model_health` `{revision_id, new: [HealthFinding]}` - right after an `edit_applied` of a model change that introduced
  model findings on the objects it touched.
- `explanation` `Explanation.to_dict()` - the answer to "why does this card show 46.76%?", "why is this bar red?", "how is X
  calculated?": the Explain panel's data (formula, steps, filters, relationships, colour). The text reply is
  `Explanation.text`, which never holds a whole formula; only that text is stored in the chat history.
- `test_suggestions` `{tests, text}` - "suggest tests": proposed tests, offered with Add (nothing is stored).
- `edit_confirm` of kind `review_fixes` - "fix it" / "apply the fixes" right after chat showed a page review: the review's
  fixes as one change (Apply sends them as one revision through `.../chat/edit-confirm`).

**Judgement messages, answered without the model** (after the edit paths, before what-ifs, card answers, data answers and
the model; order in `StudioService.chat_events`): "review this page", "check my layout", "what's wrong with this page",
"anything off", "look at what I did", "is this page ok" (the review's first 3 messages, "N more in the Review pane", and the
offer to fix); `model_health.is_health_request` ("check the model", "model health", "best practice", "BPA": `describe`,
object names only); "run the tests" (always), "check my numbers", "did anything change", "are the numbers still right" (only
when the report has tests: `describe_results`); "what tests do I have", "list the tests"; "suggest tests"; "pin this
number" (the selected card's number; a chart point or table cell is pinned from the canvas); then
`number_explain.is_explain_question` (`explanation` + text; nothing on the page fits: the message goes on as before).
"Fix it" after a review is read **before** the edit paths (the parser would hand it to the planner) and only while that
review's head is still the head. A confirmed `report_tests_broken` in chat needs the port's `confirmed` argument (see the
port below). A polite data question ("could you name the top agent by MTD sales?") is read as the question inside it before
the data-answer planner; when the question names MTD/QTD/YTD and the report has that period measure (`[MTD Total Sales]`),
the answer ends with the measure it used and whether the report's own measure gives the same numbers for the rows listed.
The text stream carries a short plain-English reply after the events (the receipt: what was done, what only Power BI shows,
what was kept as it was, `Say "undo" to take it back.`). What a message asks is handled **in message order** with these rules:
- Every APPLY clause goes into **one batch = one revision** (more than 50 operations: refused with a plain message, nothing
  applied). If an AI plan accompanies clear report clauses and the message has no history command, the clear clauses are
  held in that plan too: one preview, one Apply, one revision and one Undo. Otherwise a CLARIFY, unsupported request or
  separate measure confirmation does not hold back the clear clauses; the receipt says which were done, asked or refused.
- Undo, redo, restore, "what did you change" are the port's `undo`, `restore` (redo = restore the change a still-current undo
  took back), `history`/`what_changed`. Undo reverts the newest change only. "Restore the original" restores `r0000` and asks
  first (`edit_confirm` `restore`). An undo that comes before edits in the message runs first and the edits after it are read
  again against the report the undo left (from the user's own words, separators kept: the clause texts are never re-joined
  with a space, which would merge "make X green, make the title bigger" into one edit); each revision uses its own
  idempotency key (`<key>`, `<key>:2`). "Undo the undo" is a redo, never a second undo ("undo the undo the undo" asks the
  user to say undo or redo once).
- A step the edit layer refuses because the report already looks like that ("Step 2: ... already looks like that: nothing
  to change") is left out and the rest is applied again as a new call (`<key>:2`), with a line saying so; when it is all
  the message asked, the reply says the report is already like that (no "could not", no Desktop steps).
- A yes to `edit_confirm` sets its flag on the operation of the step that asked (`step` of the edit layer's error; every
  operation that takes the flag when the error names no step). A "(or should it be blue?)" in brackets is a second thought,
  not a second value: it is left out and the receipt says so. "What is the total, and make X green" is read as a question
  plus an edit, like the other order; the words after "to" of a title or caption edit are the text, not a question.
- A step that asked or failed holds back the head-moving steps after it ("I did not start the other changes...").
- An unsupported request is plain text: the capability's reason and the Desktop steps (no model call, no edit).
- A "what if the target were 120K" is answered locally (the card recomputed by Studio's DAX engine, no model), then a measure
  change asks for its confirmation. When the what-if was answered, the rest of the message gets no model call.
- What is left of the message (a question, "make a table of the top 5 agents" read as figures) goes to the existing question
  paths after the receipt; small talk after an edit ("Thanks!") gets no reply from the model. A question tacked on with "and"
  ("make the bars green and what is the total sales?") is read apart from the edit.
- **Planner (the 4B tier, P30; `edit_planner`).** Every operation has a tool card (`edit_tools.ToolCard`: summary, cues,
  typed slots, examples; `edit_tool_cards` for the operations of the first rounds, each family module for its own). Text
  the deterministic tier could not read (`ParseResult.unparsed`) is planned in three tiers:
  1. *Cards without the model*: for the families added after the parser, a clause whose cue phrase clearly names one card
     and whose words give every slot it needs becomes that card's operation inside `parse_edit_message` (a report
     change applies with the other clauses, a model change asks as a measure change does, a card with `confirm` asks
     with one button).
  2. *Retrieval without the model*: the cards are scored by cues, examples and the named capability (names of visuals,
     fields and pages blanked first; the selected visual's type counts); the best 3 to 6 go into the prompt.
  3. *One model call*: `generate(PLAN_SYSTEM, <request, cards with their slots, the page's visuals by number, pages,
     fields by relevance>, <a JSON schema built for this call>, max_output_tokens=256, timeout_seconds=60,
     repair_attempts=0)` returns `{kind: plan|unclear|unsupported, steps: [{tool, slots}]}` (at most 4 steps; a visual,
     page, field or table slot can only be a number from the lists; every list is numbered from 1 with no gaps). System
     + user prompt stay within 3,000 bytes (the prompt drops examples, then shortens the lists, then the cards; a page
     too big to list whose visuals the message does not name asks to narrow instead). A page's name used as a word
     ("add a forecast line") does not move the prompt to that page; "the Forecast page", "on Forecast" do.
  **Every value the model gives must be backed by the request's words, the selection, or a question**
  (`edit_planner.Evidence`): a visual must be one the request names (else the selection when it names none; else the
  words' own visual replaces it, or a question with a ready choice per visual); a page the one it names (a named page
  wins over the model's number; the page on screen only for a card that places something new or for "this page");
  a field one it names (or a year's column for "exclude 2018"); a color one of its colors; a number one of its numbers
  ("second" is 2, "to the front" is 1, "a little" a small fraction); a choice one of its words or their usual
  synonyms ("under the chart" is Bottom), or what the target implies (the well a measure goes to); a bool the on/off
  of its own words ("hide the axis title" hides the title, not the axis; "give the chart the title X" shows nothing);
  a text the request's own words and not what it says it does not want ("anything but Sales"). A value the request
  does not talk about is left out and the plan says so; one it says differently sinks the step; a required one the
  words do not give is asked (a page or a choice with one button each). At most one step per piece of the request.
  A named visual restricts the model's visual choices to the named targets, and the shortlist excludes operations that
  do not fit the target's type (chart data labels and axes versus table number formats and layout). Displayed field
  headers and native aliases are available alongside canonical names, so a visual calculation may name the header
  shown in its table; canonical model bindings are retained. Explicit filter values (such as the Store channel) are
  recovered from the words if the model omits them. Once a color-rule style is known, an omitted numeric threshold
  is recovered from an explicit cutoff in the request. These recoveries supply stated values, not inferred ones.
  "Bookmark navigator" or "page navigator" determines the navigator kind independently of the page it is placed on.
  Unrequested optional custom names, renames and format strings are excluded from the model's available settings and
  discarded if returned anyway. A delete or clear step is allowed only when a removal word governs the card's own
  object ("remove the border from the slicer" deletes
  no visual, "remove the Rank column from the table" deletes no measure); a card that adds is left out when the request
  removes or switches off what it adds; a step the request says not to do ("don't ...") is left out. A `dax`/`m` slot
  the user did not type is drafted in a second short call only when it is required or a change verb governs
  "formula"/"DAX" (never after "keep the formula"), from the natural-language request and field/measure names only.
  An `update_measure` draft also sees that measure's current formula; Power Query (M) bodies never enter a prompt.
  A typed formula goes directly to local checks without a draft call. A drafted formula is checked
  (DAX: the local parser and the model's names; M: the Power Query validator), retried once with the check's error,
  else asked for. The target measure's own name is excluded from drafting;
  self-references are refused. Every step must pass `parse_operations`. The card tier (1.) reads every registered card and
  stays out when a built-in card leads (the parser's own reading failed), when a card and its removing sibling are the
  two best, when the request names a field the card has no slot for, or when a template lacks the field it works on.
  A fresh request such as "Create a measure called Discount Share that is total DiscountAmount divided by Total Sales"
  keeps the name separate from the calculation description and routes that description to local Qwen for drafting.
  Supplying a name alone asks for the calculation; it does not invent one. A typed formula remains on the local path.
  Questions said as requests ("let me know which agent is top", "name the top agent", "could you show me the top 5
  agents", "title?", "include the table in your answer") go to the question paths, never to the planner.
  What the model planned is shown as `edit_plan` and applied only on Apply (above), through the same port as any
  edit (the editor's dry run, its `needs_confirmation`/`needs_choice` questions, receipts, Undo). Text reaches the
  planner when it asks for a change (an edit verb, a complaint) or when a tool card clearly fits it ("draw a frame
  around the slicer", "order the table by Rank"); "I need this for the board meeting" or "send this to my boss" goes
  to the question paths. Any failure, a timeout, an unclear answer or a refusal gives `edit_choices` with suggestion
  chips (`imported_chat_edits.fallback_clause`) and **applies nothing**; a refusal of a capability the matrix refuses
  says why and gives the Desktop steps. The model's own words are never shown. The model is never called when the
  deterministic tier read the message. `edit_capabilities.CAPABILITY_MATRIX` is derived from the registered cards:
  "supported" is exactly what a card provides (publishing stays refused).

**DAX Lab and the local model.** DAX Lab's prompts go only to the local Qwen model, never to an external service.
Explaining, fixing, advising on or refining a formula sends the formula in the editor, the recent discussion, the
bodies of the report measures it uses (author comments removed) and the local evaluation error to that local model;
deterministic repairs (a sibling measure's fix, planned queries, rescues) still answer first without it. A rejected
draft gets one correction turn with the specific check result and the rejected formula. Power Query (M) text is never
part of these prompts.

**Not tied to the client (P8).** The whole turn runs on its own thread (the batch itself on the report's serial worker); the
SSE generator only relays its events. If the client disconnects, the batch **still completes** and the thread records the outcome
in the chat history: `chat_actions` under the request's idempotency key (`{text, draft_id, edit_events}`), plus both turns, so
a reload shows the plain-English receipt (no JSON) and a repeated request with the same key replays the stored `edit_*` events
and then the text (the `prior` branch of `chat_events`) instead of applying twice. A client that left before anything was done
(nothing started) makes no change and records nothing. For a message with a question left over, the receipt is recorded first;
the answer to the question records its own two turns.

**Answering a question** (`edit_choices`, `edit_confirm`): each question is stored in memory (30 minutes, at most 100, keyed by
`pending_id`, per workspace) and answered once:
- `POST /v1/workspaces/{ws}/chat/edit-choice` `{pending_id, index, idempotency_key?}`
- `POST /v1/workspaces/{ws}/chat/edit-confirm` `{pending_id, accept, idempotency_key?}`
With `Accept: text/event-stream` the answer streams the chat protocol above (`edit_progress`, `edit_applied`, `token`, `done`).
Otherwise (the default) it is one JSON body when the change is done: `{message, text, receipt, events}` (`receipt` is the
`EditReceipt` or null), or, when the change failed, `{code, message, detail, receipt: null, events}` with the error's status
(`410 expired` for an unknown or expired `pending_id`: "That question has expired. Ask again ..."). A retry with the same
`idempotency_key` and the same answer while the first is still running joins it (it gets the same events) instead of finding the
question gone. An answer that does not fit
its question (a choice for a Yes/No question, an index that is not there) leaves the question open and says so; a busy report
(`edit_busy`, `desktop_busy`) keeps it open for another try. Without an `idempotency_key` the server makes one (the answer is
then not replayed when sent twice); with one, a repeated answer replays the recorded events. A confirmed answer is applied
against the head the question was read at (`head_moved` if the report changed since), the button clicked is the user's turn in
the chat history and the receipt the reply. A model-edit confirmation calls the port with `confirm_model=true`; the edit
layer's confirmations set their flag on the operations that take it (and `confirm_replace=true`); `desktop_unsaved` retries
with `desktop_work: "save"`.

### The in-process port (`ImportedEditingService.port`, `imported_editing.EditPort`)
Chat does not call HTTP; it uses `service.editing.port`, which queues on the same per-report lane as the routes and waits.
Every method is synchronous and returns the API's JSON shapes as plain dicts, or raises `ImportedEditError` (`.code`,
`.status`, `.message`, `.extra`, `.payload()`: the same codes as the routes).
```python
class EditPort(Protocol):
    def head(self, workspace_id) -> {"id", "hash"}
    def edit_context(self, workspace_id, selection, active_page) -> edit_context.EditContext
    def apply(self, workspace_id, ops, *, summary, idempotency_key, expected_head, progress=None,
              desktop_work="ask", confirm_model=False, confirm_replace=False, answers=None,
              confirmed=()) -> EditReceipt(dict)   # confirmed: ("report_tests_broken",)
    def undo(self, workspace_id, *, expected_head, idempotency_key, progress=None, desktop_work="ask") -> EditReceipt
    def restore(self, workspace_id, revision_id, *, expected_head, idempotency_key,
                confirmed_discard=False, progress=None, desktop_work="ask") -> EditReceipt
    def history(self, workspace_id) -> [history row]            # newest first
    def what_changed(self, workspace_id, limit=10) -> [history row]   # studio, desktop, undo, restore rows
    def remember_last_edit(self, workspace_id, revision_id, ops, summary) -> None
```
`progress` is called on the caller's thread with `{"stage", "label"}` (stages: `planning`, `closing_desktop`, `writing`,
`opening_desktop`, `applying`, `saving`) while the batch runs; map each to an `edit_progress` event. `apply` is what
`edit_intents` results are sent through: `ops` are the clauses' op dicts, one call per message (one revision). A
`needs_confirmation` (`kind: "model_edit"`) or `needs_choice` error is what the chat turns into `edit_confirm` /
`edit_choices`, and the confirmed retry passes `confirm_model=True` (same `idempotency_key`).
`edit_context` builds the parser's `EditContext` from the head: pages, visuals (captions, fields, position, hues), measures,
tables, `head_revision`, the newest change as `last_edit` (the newest Studio or Desktop revision, or what
`remember_last_edit` noted while that revision is still the head), and `palette` (the theme's own color words: a hue name
maps to the first data color of that hue, in data-color order).
To attach to a batch that a route started, `service.editing.status(workspace_id, batch_id)` polls it and a `BatchHandle`'s
`events(heartbeat=...)` replays its progress from the start (yielding `None` when idle); `on_complete(callback)` and
`service.editing.on_batch_finished` are called when a batch ends, which is where the chat records its outcome.

**What Studio's chat panel does with these events.** `edit_progress` drives the staged strip; `edit_applied` becomes a receipt
card (Undo enabled only while its `head.id` is still the report's head); `edit_plan` becomes a plan card, with a matching
`edit_confirm` of kind `plan` skipped; `edit_choices` and other `edit_confirm` events become question cards answered through
the two routes above; `edit_failed` becomes an error card. A `model_edit` confirmation made only of
measure operations also offers **Add to pending changes**: the operations go to the "Pending Power BI changes" tray (with the
`what_if` answer shown beside the first) so several measure changes can be applied in one Desktop cycle, and the server's
question is left to expire. After a reload the receipts of earlier turns are read back from the workspace's activity log:
each recorded turn is a `chat.action` event `{request_id, text, draft_id, edit_events: [{event, data}]}`, and only its
`edit_applied` entries are shown again (a question that was pending is answered from server memory for 30 minutes at most, and a
failure is already in the recorded reply text). "New chat" hides the receipts recorded before it.

## Canvas gestures and the Visual pane
Both send the same `edits` request. Gestures accumulate in the client and are sent once when the interaction settles
(mouse up, or ~2 s idle for arrow-key nudges) so twelve nudges make one revision. The client applies move/resize
optimistically and rolls back on a failed batch.

## Operations (the `operations` of an edit request)
A batch holds 1 to 50 operations (`report_edits.parse_operations`; a step that does not fit is `422 edit_rejected` naming the
step in plain English, an unknown `op` is `422 edit_unsupported`). `visual` and `page` name a target by its id, its caption or
its number; a miss lists the candidates. Positions are **page pixels** everywhere: a group member's stored position is relative
to its group (nested groups add up), and the edit layer converts both ways. `?` marks an optional field. Every operation is a
class in `report_edit_ops.py` (data only); the handlers are in the `report_edit_*.py` family modules.

Styling writes are unsupported: colors, titles, fonts, labels, legends, axes, backgrounds, borders,
theme changes, column appearance, button appearance, format painting and visual-header appearance.
Chat returns the `styling` capability refusal and performs no substitute edit. The canvas still renders
existing styling, themes, images and conditional formatting through the unchanged renderer contract.

**Layout and visuals** (`report_edit_layout.py`, `report_edit_align.py`, `report_edit_geometry.py`)

| `op` | Fields | Notes |
|---|---|---|
| `set_position` | `visual, x?, y?, width?, height?` | Nothing to change is an error (it used to report a move); clamped to the page with a warning |
| `arrange` | `visual, to: front\|back\|forward\|backward` | Among siblings of the same group; "already at the front" writes nothing |
| `set_visual_hidden`, `delete_visual` | `visual, hidden` / `visual` | Deleting a group deletes its members; bookmarks that named them are cleaned and the receipt says so |
| `duplicate_visual` | `visual, page?, x?, y?` | Also a group (with its members); placed in free space; new filter names |
| `add_visual` | `page, visual_type, fields, small_multiples?, x?, y?, width?, height?, title?` | Position and size are optional: omitted, the visual goes in free space at the type's default size, with an overlap advisory if the page is full. `needs_choice` when a numeric column needs an aggregation |
| `change_visual_type`, `set_fields`, `set_sort` | `visual, visual_type` / `visual, role, fields?, mode?: add\|replace\|remove` / `visual, field, direction?` | A type change carries the settings that still apply across and lists the ones dropped |
| `align_visuals` | `visuals, edge: left\|right\|top\|bottom\|center_h\|center_v, to?: selection\|page` | |
| `distribute_visuals` | `visuals (2..50, three to act), axis: horizontal\|vertical` | |
| `swap_visuals` | `a, b` | Exchange positions |
| `place_visual` | `visual, relative_to, side: left_of\|right_of\|above\|below\|align_with, gap?=10, edge?` | `edge` only for `align_with` |
| `add_textbox` | `page, text, x?, y?, width?, height?, font_family?, font_size?, color?, bold?` | |
| `add_shape` | `page, shape? (23 shapes), fill?, outline?, x?, y?, width?, height?` | A shape Studio's canvas does not draw is reported `powerbi_only` |

**Pages** (`report_edit_pages.py`)

| `op` | Fields | Notes |
|---|---|---|
| `rename_page`, `move_page`, `set_page_hidden` | `page, name` / `page, index` / `page, hidden` | Hiding every page warns |
| `add_page` | `name, width?, height?, after?` | |
| `duplicate_page` | `page, name?` | Default name "X (copy)", then "X (copy 2)"; the copy gets its own filter and binding names |
| `delete_page` | `page, cleanup_references?=false` | `needs_confirmation` `remove_page_references` while buttons or bookmarks point at it; `cleanup_references: true` removes those references |
| `set_page_size` | `page, width, height, display?, scale?=true` | With `scale` (the default) every visual box, group members included, scales with the page; `false` keeps them (and refuses if one falls outside). A stretched aspect ratio warns |

**Model** (`report_edit_measures.py`, applied through Power BI Desktop; needs `confirm_model`)

| `op` | Fields | Notes |
|---|---|---|
| `create_measure` | `table, name, expression, format_string?, folder?, description?` | The expression is the formula only; a pasted "Name = ..." definition is refused |
| `update_measure` | `name, expression?, format_string?, folder?, description?, hidden?` | `hidden` is accepted by the schema and **refused** with the Desktop steps: `pbi measure update` has no such flag, so it would be reported as done and never made |
| `rename_measure` | `name, new_name` | Every place that names it is rewritten, and dependent measures get an `update_measure` after the rename (`model_operations`) |
| `delete_measure` | `name` | Refused, listing the visuals, filters and measures that still use it |

**Filters** (`report_edit_filters.py`, `report_filters.py`; every variant Desktop-verified except fixed-decimal columns and hierarchy levels)

| `op` | Fields | Notes |
|---|---|---|
| `set_visual_filter`, `set_page_filter`, `set_report_filter` | `visual` / `page` / -, `filter, locked?, hidden?` | `filter.kind` is `categorical` (`field, values, exclude?`), `advanced` (`field, conditions: [{operator, value?}], logic?`), `top_n` (`field, n, by, direction?`) or `relative_date` (`field, unit, direction, count?, include_today?`) |
| `clear_filter` | `scope: visual\|page\|report, target?, field?` | One field, or every filter at that scope |

The four families below joined the facade in round 3 (2026-09-29). Each example is the first example of the operation's tool
card (the Agents dashboard: page `1`; `e82e02f93055d6b0b544` is its table of agents, `b404a461ede7604a2774` the 'Total MTD
Sales' chart). Shapes no Desktop-authored file confirms are listed in each module's `UNVERIFIED_SHAPES` and decided by its
`DESKTOP_PROBES`.

**Conditional formatting, number formats and table layout** (`report_edit_conditional.py`)

| `op` | Fields | Notes and example |
|---|---|---|
| `set_conditional_format` | `visual, style: gradient\|rules\|field_value\|data_bars\|icons, target?, field?, based_on?, summarization?, apply_to?, min_color?, mid_color?, max_color?, min_value?, mid_value?, max_value?, blank?, blank_color?, rules?, measure?, positive_color?, negative_color?, axis_color?, direction?, bar_only?, icon_set?, icon_position?, icon_alignment?, replace_conditional?` | Overwriting existing conditional formatting stops with `needs_confirmation` `replace_conditional` (receipt `replaced`); a partial edit of a scale, data bars or icon placement edits in place. `rules` items: `{color\|icon, min?, max?, min_inclusive?, max_inclusive?, unit: number\|percent, equals?, blank?}`; date fields refused, text fields only `equals`/`blank`. `{"op": "set_conditional_format", "visual": "e82e02f93055d6b0b544", "style": "gradient", "target": "background", "field": "Rank", "min_color": "#DEEFFF", "max_color": "#118DFF"}` |
| `clear_conditional_format` | `visual, target?, field?` | Never removes measure bindings such as a card's image or reference labels. `{"op": "clear_conditional_format", "visual": "e82e02f93055d6b0b544", "target": "font_color", "field": "Rank"}` |
| `set_number_format` | `visual, target?: data_labels\|value_axis\|card_value\|column\|field, field?, preset?, format?, display_units?, decimals?, currency_symbol?, replace_conditional?` | Prefers the shapes Desktop always keeps (column units and decimals, a card's custom format); a visual-level format string (currency or percent on a table column, pie/donut/waterfall/gauge labels, the old card) carries a receipt warning. `{"op": "set_number_format", "visual": "b404a461ede7604a2774", "target": "data_labels", "preset": "thousands", "decimals": 1}` |
| `set_table_layout` | `visual, totals?, row_subtotals?, column_subtotals?, stepped?, layout?, values_on_rows?, replace_conditional?` | Structural table and matrix settings only. `{"op": "set_table_layout", "visual": "e82e02f93055d6b0b544", "totals": true}` |
| `move_column` | `visual, field, to_index` | `to_index` counts from 0 (the planner's card takes a 1-based place). `{"op": "move_column", "visual": "e82e02f93055d6b0b544", "field": "Rank", "to_index": 0}` |

The colored up/right/down arrow set is drawn beside table values in Studio's preview;
right-hand placement is stored natively as `After`. The preview reflects Apply, Undo and
Redo after its data refresh. The icon edit remains part of the same report revision.

**Navigation and interactivity** (`report_edit_navigation.py`; its cards assume `EXAMPLE_SETUP`: pages 'Agent details',
'Agent tooltip', 'Stores', 'Spare', bookmarks 'Overview' and 'Top agents' in group 'Views', a 'Details' button, a bookmark
navigator)

| `op` | Fields | Notes and example |
|---|---|---|
| `add_bookmark` | `name, page?, data?, display?, current_page?, visuals?, hide?, show?, group?` | `data` (filters, slicers, sorts, drill) is off unless asked. `{"op": "add_bookmark", "name": "Overview page", "page": "1"}` |
| `update_bookmark` | `bookmark, recapture?, data?, display?, current_page?, visuals?` | Keeps what the bookmark hides and shows for visuals that survive. `{"op": "update_bookmark", "bookmark": "Overview"}` |
| `rename_bookmark` | `bookmark, name` | `{"op": "rename_bookmark", "bookmark": "Overview", "name": "Summary"}` |
| `move_bookmark` | `bookmark, position?, group?, top_level?` | `{"op": "move_bookmark", "bookmark": "Top agents", "position": 1}` |
| `group_bookmarks`, `ungroup_bookmarks` | `bookmarks, name` / `group` | An emptied group is removed. `{"op": "group_bookmarks", "bookmarks": ["Overview", "Top agents"], "name": "Pages"}` |
| `delete_bookmark` | `bookmark, cleanup_references?=false` | `needs_confirmation` `remove_bookmark_references` while buttons or navigators use it. `{"op": "delete_bookmark", "bookmark": "Overview"}` |
| `set_bookmark_visual_hidden` | `bookmark, visual, hidden?` | `{"op": "set_bookmark_visual_hidden", "bookmark": "Overview", "visual": "e82e02f93055d6b0b544"}` |
| `add_button` | `page, kind?: blank\|back\|bookmark\|information\|help\|qna\|left_arrow\|right_arrow\|reset, text?, action?, tooltip?, x?, y?, width?, height?` | `action` is `{type: back\|bookmark\|page\|drillthrough\|web_url\|qna\|none, bookmark?, page?, url?}`. A back button defaults to the native Back action, with no destination page. `{"op": "add_button", "page": "1", "kind": "back"}` |
| `set_button_action` | `visual, action?, tooltip?` | `tooltip: ""` removes the tooltip; `tooltip` left out or null leaves it as it is (a tooltip needs an action). `{"op": "set_button_action", "visual": "Details", "action": {"type": "page", "page": "Stores"}}` |
| `add_navigator` | `page, kind?: page\|bookmark, orientation?, rows?, columns?, bookmark_group?, show_hidden_pages?, show_tooltip_pages?, x?, y?, width?, height?` | `{"op": "add_navigator", "page": "1", "kind": "page"}` |
| `set_navigator` | `visual, orientation?, rows?, columns?, bookmark_group?, deselection_bookmark?, show_hidden_pages?, show_tooltip_pages?, show_pages?, hide_pages?` | `{"op": "set_navigator", "visual": "bookmark navigator", "rows": 2, "columns": 2}` |
| `set_drillthrough` | `page, fields, mode?: replace\|add\|remove, keep_all_filters?, cross_report?, back_button?` | `{"op": "set_drillthrough", "page": "Agent details", "fields": [{"table": "DimEmployee", "name": "EmployeeKey"}]}` |
| `set_tooltip_page` | `page, size?: single_visual\|multiple_visuals\|mini_report\|keep, hide?, fields?, mode?` | `{"op": "set_tooltip_page", "page": "Spare"}` |
| `clear_page_type` | `page, cleanup_references?=false` | Back to an ordinary page; `needs_confirmation` `clear_page_links` while buttons or visual tooltips still use it; `cleanup_references: true` removes those links. `{"op": "clear_page_type", "page": "Agent details"}` |
| `set_visual_tooltip` | `visual, page?` | `{"op": "set_visual_tooltip", "visual": "b404a461ede7604a2774", "page": "Agent tooltip"}` |
| `set_interaction` | `source, targets?, type: filter\|highlight\|none\|default` | `{"op": "set_interaction", "source": "54be5cf0783e66471540", "targets": ["b404a461ede7604a2774"], "type": "none"}` |
| `reset_interactions` | `visual?, page?` | `{"op": "reset_interactions", "visual": "54be5cf0783e66471540"}` |
| `sync_slicer` | `visual, pages?, hidden_on?, group?, field_changes?, filter_changes?` | `visual` is the source slicer; `pages` names destinations. A suitable slicer there joins the group, otherwise Studio adds a synchronized copy. In chat, a destination page does not move source-slicer resolution away from its source page; visible or recognized field names can identify the source. `{"op": "sync_slicer", "visual": "5b0dbccd61c6c4adb415", "pages": ["Stores"]}` |
| `unsync_slicer` | `visual, pages?, delete_copies?` | `{"op": "unsync_slicer", "visual": "b85aa81a8b5c60030d13"}` |
| `set_drill_filter` | `visual?, filter_other_visuals` | Without `visual`, the report default. `{"op": "set_drill_filter", "visual": "b404a461ede7604a2774", "filter_other_visuals": false}` |
| `drill_visual` | `visual, action: next_level\|expand_all\|up\|top, role?` | Column and hierarchy-level wells only. `{"op": "drill_visual", "visual": "b404a461ede7604a2774", "action": "next_level"}` |

**Analytics, phone layout, groups and other visual features** (`report_edit_extras.py`)

| `op` | Fields | Notes and example |
|---|---|---|
| `set_analytics_line` | `visual, kind: constant\|x_constant\|min\|max\|average\|median\|percentile\|trend\|forecast\|error_bars\|ratio\|symmetry_shading, line?, new?, measure?, value?, percentile?, name?, show?, color?, style?, width?, transparency?, position?, data_label?, label_text?, label_color?, label_horizontal?, label_vertical?, label_decimals?, label_units?, shade?, shade_color?, shade_transparency?, band?, upper?, lower?, upper_color?, lower_color?, combine_series?, forecast_length?, ignore_last?, seasonality?, confidence?` | `value` is a number, or `YYYY-MM-DD` on a date axis. Forecast needs one value, no legend and a date or number axis. `{"op": "set_analytics_line", "visual": "b404a461ede7604a2774", "kind": "constant", "value": 50000, "name": "Target", "color": "#D64550", "style": "dashed"}` |
| `remove_analytics_line` | `visual, kind?, line?, all?` | `{"op": "remove_analytics_line", "visual": "b404a461ede7604a2774", "line": "Target"}` |
| `place_on_mobile` | `visual, x?, y?, width?, height?` | The phone canvas is 320 px wide; groups and grouped visuals are refused. `{"op": "place_on_mobile", "visual": "5b0dbccd61c6c4adb415", "x": 0, "y": 0, "width": 320, "height": 60}` |
| `remove_from_mobile` | `visuals?, page?, all?` | `{"op": "remove_from_mobile", "visuals": ["e82e02f93055d6b0b544"]}` |
| `arrange_mobile_layout` | `page, columns?: 1..3, visuals?, gap?` | `{"op": "arrange_mobile_layout", "page": "1"}` |
| `group_visuals` | `visuals, name?, mode?: scale\|scroll` | `{"op": "group_visuals", "visuals": ["fb4c5aa34e0b084b62b9", "2202e311b1493069e4eb"], "name": "KPI cards"}` |
| `ungroup_visuals` | `group, cleanup_references?` | `needs_confirmation` `cleanup_references` when a bookmark remembers the group or it is hidden. `{"op": "ungroup_visuals", "group": "KPI cards"}` |
| `set_group` | `group, name?, mode?` | `{"op": "set_group", "group": "KPI cards", "name": "Headline numbers"}` |
| `set_visual_calculation` | `visual, name?, expression?, template?, field?, window?, new_name?, role?` | `{"op": "set_visual_calculation", "visual": "e82e02f93055d6b0b544", "name": "Running MTD Sales", "template": "running_sum", "field": "MTD Sales"}` |
| `remove_visual_calculation` | `visual, name` | `{"op": "remove_visual_calculation", "visual": "e82e02f93055d6b0b544", "name": "Running MTD Sales"}` |
| `set_small_multiples` | `visual, field?, remove?, rows?, columns?, shared_y_axis?, titles?` | `{"op": "set_small_multiples", "visual": "b404a461ede7604a2774", "field": {"table": "DimChannel", "name": "ChannelName"}}` |
| `set_alt_text` | `visual, text` | `{"op": "set_alt_text", "visual": "e82e02f93055d6b0b544", "text": "Agents ranked by MTD sales"}` |
| `set_tab_order` | `visuals?, page?, by_position?` | `{"op": "set_tab_order", "visuals": ["b85aa81a8b5c60030d13", "54be5cf0783e66471540"]}` |
| `set_keep_layer_order` | `visuals, keep?` | `{"op": "set_keep_layer_order", "visuals": ["8a8ba163374a72762030"], "keep": true}` |
| `set_page_view` | `page, view: fit_to_page\|fit_to_width\|actual_size` | `{"op": "set_page_view", "page": "1", "view": "fit_to_width"}` |
| `set_field_display_name` | `visual, field, name` | The name in one visual only (`displayName` and `nativeQueryRef`, as Desktop writes it); a table's column header. `{"op": "set_field_display_name", "visual": "e82e02f93055d6b0b544", "field": "MTD Sales", "name": "Sales this month"}` |

Lock objects (View > Lock objects) is not an operation: Desktop keeps that switch in the view, not in the report.

**The model beyond measures** (`report_edit_model.py`; applied through Power BI Desktop by Studio's TOM runner, needs
`confirm_model`; every card needs Desktop and a yes)

| `op` | Fields | Notes and example |
|---|---|---|
| `create_calculated_column` | `table, name, expression, format_string?, data_type?, summarize_by?, folder?, description?, hidden?` | `{"op": "create_calculated_column", "table": "FactSales", "name": "Margin", "expression": "FactSales[SalesAmount] - FactSales[TotalCost]", "format_string": "#,0.00"}` |
| `set_column_properties` | `table, name, format_string?, data_type?, sort_by?, data_category?, summarize_by?, folder?, description?, hidden?, key?, expression?` | `data_type` and `expression` only on a calculated column; `sort_by: ""` stops sorting by another column. `{"op": "set_column_properties", "table": "Date", "name": "Month", "sort_by": "Month Number"}` |
| `rename_column`, `rename_table` | `table, name, new_name` / `name, new_name` | The report's references are rewritten in the second stage (`apply_report_operations`) and the model's formulas in the same Desktop save. `{"op": "rename_column", "table": "DimStore", "name": "StoreName", "new_name": "Store"}` |
| `delete_column`, `delete_table` | `table, name` / `name` | Refused while visuals, filters, formulas, relationships, sort-by columns or hierarchy levels use it. `{"op": "delete_column", "table": "DimStore", "name": "Status"}` |
| `create_calculated_table` | `name, expression, description?, hidden?` | `{"op": "create_calculated_table", "name": "Years", "expression": "DISTINCT('Date'[Year])"}` |
| `set_table_properties` | `table, hidden?, description?, expression?` | `{"op": "set_table_properties", "table": "DimChannel", "hidden": true}` |
| `mark_date_table` | `table, column?` | `column` null unmarks. `{"op": "mark_date_table", "table": "Date", "column": "Date"}` |
| `create_relationship`, `update_relationship`, `delete_relationship` | `from_table, from_column, to_table, to_column` + (create/update) `cardinality?, cross_filter?, active?, assume_referential_integrity?, security_filter?` | `from_*` is the many side; a second active path is refused. `{"op": "create_relationship", "from_table": "DimStore", "from_column": "EmployeeKey", "to_table": "DimEmployee", "to_column": "EmployeeKey"}` |
| `create_hierarchy`, `update_hierarchy`, `delete_hierarchy` | `table, name, levels` / `table, name, levels?, new_name?` / `table, name` | `levels` items `{column, name?}`. `{"op": "create_hierarchy", "table": "Date", "name": "Date Hierarchy", "levels": [{"column": "Year"}, {"column": "Quarter"}, {"column": "Month"}]}` |
| `create_field_parameter`, `update_field_parameter` | `name, fields` | `fields` items `{table, name, label?}`. `{"op": "create_field_parameter", "name": "Sales Metric", "fields": [{"table": "# Measures", "name": "Employees Avg MTD Sales", "label": "Avg sales"}]}` |
| `create_calculation_group` | `name, items, precedence?, column?, description?` | `items` `{name, expression, format_string_expression?}`. `{"op": "create_calculation_group", "name": "Time Intelligence", "items": [{"name": "Current", "expression": "SELECTEDMEASURE()"}], "precedence": 10}` |
| `set_calculation_item`, `delete_calculation_item`, `update_calculation_group` | `group, name, expression?, format_string_expression?, ordinal?, new_name?` / `group, name` / `name, precedence?, description?` | `{"op": "set_calculation_item", "group": "Time Calc", "name": "PY", "expression": "CALCULATE(SELECTEDMEASURE(), SAMEPERIODLASTYEAR('Date'[Date]))"}` |
| `create_role`, `update_role`, `delete_role` | `name, filters?, description?, members?` / `name, new_name?, filters?, description?, members?` / `name` | `filters` items `{table, expression}`; `members` is always refused (people are added in the Power BI service). `{"op": "create_role", "name": "Store Managers", "filters": [{"table": "DimStore", "expression": "[StoreType] = \"Store\""}]}` |
| `set_table_query` | `table, expression, if_refresh_fails?: ask\|keep\|undo` | Not offered to the planner. A failed reload asks (`needs_choice`) unless told to keep or undo. `{"op": "set_table_query", "table": "FactSales", "expression": "let Source = ... in Kept"}` |
| `set_named_expression`, `delete_named_expression` | `name, expression, description?` / `name` | `set_named_expression` is not offered to the planner. `{"op": "delete_named_expression", "name": "Source Folder"}` |
| `set_query_parameter` | `name, value, type?, description?` | `{"op": "set_query_parameter", "name": "Region", "value": "East"}` |
| `refresh_data` | `table?` | `{"op": "refresh_data", "table": "FactSales"}` |
| `set_model_options` | `discourage_implicit_measures?, culture?, auto_date_time?` | `culture` and `auto_date_time` are refused with the File > Options steps. `{"op": "set_model_options", "discourage_implicit_measures": true}` |

Operations Microsoft does not list for external tools (renaming columns and tables, hierarchies, date tables, Power Query,
refresh, model options) run only when a passing model probe receipt for the installed Desktop version covers them
(`report_edit_model.MICROSOFT_SUPPORT`, `OUTSIDE_SUPPORT`); otherwise they are refused before Desktop opens, with Desktop's steps.

## The edit layer as a Python API (`report_edits`)
The chat, the service and the tests call these; nothing here uses the network or Power BI Desktop.
- `parse_operations(raw) -> list[ReportOperation]`: dicts to typed operations (1 to 50).
- `apply_operations(document, operations, model) -> EditOutcome`: applies every operation or none (the first failure raises
  `DocumentError`; nothing is written until `document.commit`). `model` is the model before the batch. `outcome.model_operations`
  lists what Desktop must apply, in order: each model operation (`report_edits.MODEL_OPERATIONS`: the measure operations and the
  `model` family) followed by the operations its handler adds. A model operation sets `outcome.report_changed` only when it has
  a report half (a rename).
- `apply_report_operations(document, operations, model) -> EditOutcome`: the **second stage** of a batch that has model
  operations, run on the file Desktop saved after applying them. It applies every ordinary operation, and of a model operation
  only what it does to the report (`REPORT_HALF_HANDLERS`: renaming a measure, a column or a table rewrites the references; every
  other model operation does nothing there). `model` is the model after the model operations and `outcome.model_operations`
  stays empty.
- `capture_baseline(document, model=None) -> set[(code, part, pointer)]` records the findings the head already has, taken before
  a batch runs. `validate_findings(document, *, model=None, baseline=None, lineage=None) -> list[Finding]` returns typed
  `Finding(code, severity, part, pointer, message)`; `validate_document(...)` is the same checks as plain-English blocking
  messages. With `baseline`, a problem the source report already had never blocks an edit; `lineage` (new name -> source name)
  holds a copied visual or page to its source's baseline. Blocking codes mean Desktop could not open the file or would show
  something broken; advisory codes (`visuals_overlap`, `size_below_minimum`, `tab_order_gap`, `tab_order_duplicate`,
  `powerbi_only_visual`, `schema_version_unknown`) never block. Filter objects are also checked against Microsoft's
  filterConfiguration 1.3.0 and semanticQuery 1.4.0 schemas, packaged in `studio/pbir_schemas/` (skipped when `jsonschema` or the
  files are missing).
- `ModelInfo.from_report(tables, measures, types=None, *, calculated=None)`: the model facts the handlers check against
  (columns, measures with their DAX, data types, hidden flags, field-parameter tables). `types` is the `pbix_extract` types
  payload; `calculated` maps `Table[Column]` or `Table` to its DAX (calculated columns and tables), so a rename or delete sees
  those dependents.

`HandlerResult` (what one handler returns; `EditOutcome` sums them over a batch):

| Field | Meaning |
|---|---|
| `summary` | The one plain sentence for the receipt |
| `warnings` | Advisories to show with it |
| `protected` | `[Protected(visual, what)]`: formatting left alone on purpose |
| `replaced` | `[str]`: what a whole-visual write overwrote |
| `visible_in` | `[VisibleIn(op, where, detail)]` |
| `model_operations` | Operations the handler needs applied through Desktop after the report change (a rename's dependents); run with the batch |
| `pending_model_operations` | Operations it only suggests (colors inside DAX); shown, never applied |
| `partial` | The handler did part of what was asked, by design (`warnings` say what it left): the reply says "partly", never "done" |
| `lineage` | `{new name: source name}` for visuals and pages it copied |

## Open requests
Things the code does not do yet, so the contract above does not promise them:
- The workspace events route returns at most 250 events per request, oldest first. Studio's client follows `?after=<last
  sequence>` until a short page comes back, so it reads the newest chat receipts back after a reload; another client has to
  page the same way (or the route could return the newest 250).
