# Editing imported Power BI dashboards

Status: **Implemented; styling edits removed** (2026-10-02). Studio edits visuals, pages, layout,
fields, filters, measures and the model, plus conditional formatting, number formats and interactivity.
The renderer preserves existing styling. Styling changes belong in Power BI Desktop's Format pane.
The API and user guide describe the current supported operations; experiments and measured decisions
below are historical implementation evidence, including styling experiments from before its removal.

Read this together with:
- `imported-report-editing-review.md`, the adjudicated design review (`P1`..`P39`). **It wins over this file where they
  conflict.** The sections below were corrected to match it and the built code; the ones the review overrode say so.
- `imported-report-editing-api.md`, the HTTP, chat and Python contract of what was built (routes, errors, receipts,
  operations).
- `imported-report-render-contract.md`, the payload the canvas draws from.
- `imported-report-editing-user-guide.md`, what a Power BI user can say or click, in plain words.

The live Desktop checks that still need a human (drag and resize in a real browser, the packaged app, a full model-edit cycle
on the installed Desktop) are listed in "Not verified yet" at the end.

## What is where

| Module (`src/ai_powerbi_analyst/studio/`) | Responsibility |
|---|---|
| `pbix_package.py` | Reads a PBIX within bounded sizes; writes an edited copy that changes only what Studio changed (DataModel stored and byte-identical, `SecurityBindings` dropped) |
| `pbir_document.py` | The editable report definition: pages, visuals, resources, theme; commits only the parts that changed, as compact UTF-8 JSON; resource registration with reference counting |
| `pbir_expr.py`, `pbir_catalog.json` | The expression codec (literals, colors, selectors, state selectors) and the offline per-visual-type formatting catalog (`instanceSelectors` included) |
| `pbix_diff.py` | Semantic comparison of two PBIX files (parsed JSON, model metadata), used to adopt Desktop saves and to check that Desktop kept what Studio wrote |
| `report_edit_ops.py` | Every base operation as a closed data class; no handlers |
| `report_edits.py` | The facade: `parse_operations`, `apply_operations`, `apply_report_operations`; registers the families below |
| `report_edit_context.py` | What families share: `ModelInfo`, `HandlerResult`, `EditOutcome`, `NeedsConfirmation`, `NeedsChoice`, `VisibleIn`, `Protected` |
| `report_edit_conditional.py` | Conditional formatting, visual number formats, column ordering and structural table layout |
| `report_edit_layout.py`, `report_edit_align.py`, `report_edit_geometry.py` | Placing, stacking, hiding, deleting, duplicating, adding visuals, fields and sort; align, distribute, swap, place, text box, shape; page-pixel geometry |
| `report_edit_pages.py` | Pages and page size |
| `report_edit_measures.py` | Measure operations (through Desktop): checks, rename dependents, delete guard |
| `report_edit_filters.py`, `report_filters.py` | Filters at visual, page and report scope, written the way Desktop writes them |
| `report_edit_validation.py`, `pbir_schemas/` | Structural validation of every revision (`validate_findings`, `capture_baseline`), including Microsoft's filter schemas |
| `report_refs.py`, `report_roles.py`, `report_captions.py` | Every place a name is used; where fields go in a visual; the caption a user sees and where it is stored |
| `report_tokens.py`, `report_props.py`, `report_theme.py`, `report_formatting.py`, `live_report.py` | The renderer's side: typed style tokens, property resolution by specificity, theme merging, fidelity, the page payload |
| `report_revisions.py` | The whole-file revision chain: history, placement of Desktop's working copy, adoption, undo and restore, pruning |
| `edit_journal.py` | Write-ahead journal of a batch (crash recovery) |
| `report_editor.py` | One locked batch per request: dry run, Desktop lifecycle, one revision, receipt |
| `desktop_session.py`, `desktop_uia.py` | Power BI Desktop for one working copy: identity by process, Save through UI Automation, model operations, previews; never kills a process |
| `report_delivery.py` | "Save a copy" and "Replace original" |
| `imported_editing.py`, `imported_format_catalog.py` | The service around all of it: per-report serial worker, idempotency, capabilities, recovery, receipts, the Visual pane's catalog |
| `edit_intents.py`, `edit_parser.py`, `edit_visual.py`, `edit_targets.py`, `edit_ops.py`, `edit_capabilities.py` (and `edit_text/types.py`) | Deterministic reading of a chat message into operations, and the capability matrix of what Studio cannot do |
| `imported_chat_edits.py` | The chat turn: progress, receipts, questions, the 4B fallback tier, recording in the history |
| `apps/desktop/src/ImportedEditor.tsx` and its panes (`FormatPane`, `FieldsPane`, `PageTabs`, `HistoryPane`, `MeasureEditor`, `PendingModelChanges`, `DeliveryControls`, `ImportedOverlay`, `EditChatCards`), `importedEditing.ts`, `importedFormat.ts`, `importedGeometry.ts` | The editing UI and its pure logic; FormatPane is labeled Visual |
| `apps/desktop/src/InteractiveReportPage.tsx`, `reportRender.ts`, `reportContent.tsx` | The canvas: draws from the payload's tokens, hit-testing, edit mode, formatting-only refresh |
| `evals/imported_edit/` | The chat edit cases and the Desktop contract script (`desktop_contract.py`) |

## Evidence this design rests on (Desktop 2.157.1354.0, Agents Performance PBIX)

All of these were measured on scratch copies (scratchpad `editexp/`).

| Question | Result |
|---|---|
| Does Desktop open a PBIX whose `Report/definition/**` JSON Studio rewrote? | Yes. The title, a literal bar color and the page background changed (exp1). A visual was deleted, a card added and a custom theme registered (exp3). A chart with no explicit colors took the theme's dataColors[0] (exp4). |
| Does `SecurityBindings` have to go? | No, Desktop opened the file with it kept (exp4) and with it dropped (exp1, exp3). A Desktop save writes a fresh one either way. The writer drops it, as common tooling does. |
| Can Studio write the PBIX while Desktop has it open? | No. Desktop locks the file: `open(r+b)` and `os.replace` both fail with access denied. Report edits need Desktop closed. |
| Do model edits persist? | Yes. A `pbi measure create` over TOM sets `hasUnsavedChanges`. A UI Automation Invoke on Desktop's `Save` button (AutomationId `save`, no focus needed) writes the file in about 3 s and clears the flag. PBIXRay then reads the new measure, with all 68 column cardinalities unchanged. |
| Can Studio save with keystrokes? | Unreliable. SendKeys Ctrl+S from a background process did not save (focus lock). Use UI Automation. |
| Is there a bridge save or close method? | No. The manifest has only `application.state.get`, `report.snapshot.capture` and `file.reload` (PBIP only). |
| What does a Desktop save do to the file? | It rewrites the whole PBIX. DataModel bytes change without any model change, and unreferenced resources are deleted (the background PNG in exp1). Revisions must be whole files. |
| Does `hasUnsavedChanges` mean the user has unsaved work? | **No** (corrected by review P2/P4). Desktop turns the flag true about 10 s after opening a Studio-written file with no action at all, as it normalizes the file. The flag is therefore never treated as user work: Studio never saves Desktop's state silently, and never closes a Desktop that shows unsaved changes or a dialog. |
| How long does each Desktop step take? | Bridge `open` connects in about 20 s. A bridge `screenshot` of a PBIX page takes 0.8 s. `pbi measure create` takes 2.4 s. UIA save takes 3 s in most runs and up to 5.5 s. Close after a save takes 1.5 s. |
| Can the "Save changes?" prompt be answered by UI Automation? | No: it is an embedded web view, and after `WM_CLOSE` only the disabled main window is reachable. So Studio closes Desktop only when nothing is unsaved, and there is no kill fallback anywhere (a test greps the Desktop modules for one). A closing Desktop that asks is left exactly as it is and the user is told. |
| Which Desktop experiments back the rest of this design? | exp1, exp3 and exp4 (the rows above: rewritten report JSON, deleted visual, added card, custom theme, default series color), **exp5** (ThemeDataColor ids and shading), **exp6** (visual groups), **exp7 and exp8** (30 filter variants). Their results are in "Measured decisions" at the end, which overrides anything above. `evals/imported_edit/desktop_contract.py` repeats the contract checks (its step 8 is exp5) so a new Desktop version can be checked with one run. |
| Is `pbi-cli`'s report layer safe to use? | No, it is lossy on Desktop-authored JSON: set-background drops the image, set-container drops title styling, and format clear wipes all objects. Studio owns its PBIR writer. |
| Can the PBIX be converted to PBIP instead? | No. The model's sources (CSV on F:) do not exist, so the data lives only in the PBIX DataModel. Studio stays on PBIX. |

## Architecture: a whole-file revision chain on the PBIX

```
original.pbix (immutable, integrity-checked)
revisions/r0001.pbix … rNNNN.pbix   whole files; head = current
desktop/Report.pbix                 Desktop's working copy = copy of head
```

The pieces, from the file up to chat:

1. **`pbix_package`** reads every part within bounded sizes. It writes a new PBIX that:
   - copies unchanged parts byte-for-byte, in their original order and compression;
   - keeps DataModel STORED and identical;
   - drops SecurityBindings and its `[Content_Types]` Override;
   - adds Defaults for new extensions;
   - validates part names;
   - writes atomically and returns the file's hash.
2. **`pbir_document`** is the editable document: report.json, pages.json, page.json, visual.json and the theme and image resources. Every other part is preserved untouched. Changed parts are serialized as compact UTF-8 JSON with no BOM, which is what Desktop writes.
3. **`pbir_expr`** is the expression codec:
   - literals with `''` escaping and `D`/`L` suffixes, booleans and datetimes;
   - solid colors and `ThemeDataColor{ColorId, Percent}`. **Corrected by exp5:** ColorId 0 is always `#FFFFFF` and 1 is always `#000000` (not the theme's background and foreground), 2..9 are `dataColors[0..7]`, and 10 and up are invalid (Desktop paints the default fill). Only 0..9 are written. The first version of this design read ColorId 0 as the theme background from the grey "Others" bars under a white theme, which was wrong;
   - selectors (default, metadata queryRef, dataViewWildcard, and scopeId with typed literals).
4. **`pbir_catalog.json`** (read through `pbir_expr.catalog()`) is an offline snapshot of powerbi-report-author's per-visual-type formatting objects, properties, types and enums, plus the visuals that only honor formatting under a state selector. It preserves metadata the renderer needs and drives the retained Visual pane controls (`imported_format_catalog.py`). It ships in the packaged app (`scripts/build-desktop-sidecar.ps1` adds it as data).
5. **`report_edits`** holds the typed, closed operations (Pydantic discriminated union of base and family classes). `apply_operations(document, ops, model)` returns an `EditOutcome`: the changed parts plus a one-line summary per operation, and what was protected, replaced, where each change is visible and which model changes are only suggested. The operations, by family (fields are in `imported-report-editing-api.md`):
   - Layout: `SetPosition`, `Arrange`, `SetVisualHidden`, `DeleteVisual`, `DuplicateVisual`, `AddVisual`, `ChangeVisualType`, `SetFields`, `SetSort`, `AlignVisuals`, `DistributeVisuals`, `SwapVisuals`, `PlaceVisual`, `AddTextbox`, `AddShape`.
   - Page: `RenamePage`, `AddPage`, `DuplicatePage`, `DeletePage` (with `cleanup_references`), `MovePage`, `SetPageSize` (with `scale`), `SetPageHidden`.
   - Conditional formatting and numbers: rule and field-driven colors, data bars and icons, visual number formats, column ordering and structural table layout.
   - Navigation and extra features: bookmarks, buttons and actions, navigators, interactions, synced slicers, drillthrough and tooltip pages, analytics, phone layout, groups, visual calculations, small multiples, alt text, tab order and field display names.
   - Filters: `SetVisualFilter`, `SetPageFilter`, `SetReportFilter`, `ClearFilter` (categorical, advanced, Top N, relative date).
   - Model (applied through Desktop): `CreateMeasure`, `UpdateMeasure` (expression, format string, folder, description; `hidden` is refused with the Desktop steps because `pbi measure update` cannot do it), `RenameMeasure` (also rewrites every reference and dependent formula), `DeleteMeasure` (refused while a visual, filter or measure still uses it).
6. **`report_revisions`** keeps the revision rows: id, parent, sha256, author `import|studio|desktop|undo|restore`, operations and summary. Every version is a whole PBIX stored once under its hash (`revisions/<sha>.pbix`), `original.pbix` is revision r0000 and is never written, and the newest 40 distinct versions are kept (head, the placed revision and the parents of unfinished batches are pinned). The ImportedReport carries the head. Every reader switches from `archived_path` to the head revision with the head's hash: live_report, report_formatting caches, report_engine, card answers and chat context. After each revision, pages are re-derived with `read_report`, and measures and format strings are re-extracted when the DataModel bytes changed. Undo moves the head to the newest change's parent; that move is itself a new revision, so history is never lost. Undo is offered only on the newest change (review P21); older rows offer "Restore to before this", which lists the later changes and asks.
7. **`desktop_session`** controls Desktop for one report's working copy (review P1, P2, P4, P5, P9):
   - Desktop is identified by a new `PBIDesktop` pid whose `currentFilePath` is the working copy, plus its process create time, re-checked before every save, close, screenshot and model edit. The pid the bridge's `open` prints is never used (it can belong to another instance);
   - `status`, page screenshots, and TOM through the msmdsrv port;
   - model operations through `pbi` with a DAX probe after each; a failed model edit is rolled back with the inverse TOM write, never by discarding;
   - save through UI Automation, then wait for the file write, a clean zip and a file that parses;
   - a graceful close (`WM_CLOSE`) only when nothing is unsaved and no dialog is open;
   - **it never kills a process**, and never saves or closes over the user's own unsaved work: "Power BI Desktop has unsaved changes" is a question (Save into history and continue / Cancel), and the answer "save" is the user's;
   - Desktop is launched outside the sidecar's kill-on-close job where the job allows it, so a sidecar restart does not close it.

   The working copy is tracked ("placed"): the revision Studio last wrote into `desktop/Report.pbix`. A working copy with any other content is a Desktop save and is adopted as a revision before Studio writes over it (review P3). "Open in Power BI" adopts, then places the head, then opens it.

   **A batch is one transaction with one head move** (review P5; this replaces the earlier mixed-batch order, which saved the user's work before adopting it and committed the report layer before the model layer was proven). A batch with only report operations writes one revision with Desktop closed, or closes Desktop first when it is open and clean. A batch with model operations runs in this order:
   1. If the tracked Desktop has user work, ask (never save silently); adopt a saved copy as a revision only after Desktop is closed.
   2. Apply the model operations on the current head in Desktop (`create/update/rename/delete measure`), check Desktop's own error state and probe with `EVALUATE`.
   3. Save through UI Automation and survive-check the saved file against what Studio wrote (`pbix_diff`).
   4. Apply the report-layer operations to the saved file with `apply_report_operations` (a rename's reference rewrite included), validate it against a baseline of the head's own findings.
   5. Commit **one** revision holding both layers and move the head once; reopen Desktop if it was open before.

   On any failure the head does not move, the staging file is deleted, Desktop is rolled back with compensating TOM writes, and the reply says "partial" or "failed", never "updated". Each step is journaled before its side effect (`edit_journal`), so a crash or restart finishes or undoes the batch at start-up.
8. **Chat edits** (`edit_intents`, `imported_chat_edits`) work in two tiers, and a chat message is one batch and one revision:
   - A deterministic parser (`edit_intents.parse_edit_message`, no model) covers the frequent phrasings: recolor, title, font size, background, move, resize, align, add, delete, duplicate, hide, order, fields, sort, filters, rename page, number format, theme, undo, redo, restore and "what did you change", and a change to a threshold or measure. It resolves targets from the visual selected in the UI, captions, field names and type-and-position words. A step that clarifies, asks or is unsupported never holds back the clear steps of the same message.
   - Text the deterministic tier cannot read gets **one** attempt with the guarded 4B model (`edit_intents.model_index`: a compact visual index, at most 3,000 bytes with the instructions, 60 s), whose answer is validated like any other operations. A timeout, an invalid answer or a refusal gives suggestion chips and **applies nothing**. The model is never called when the deterministic tier read the message.

   Ambiguity gets one clarifying question that offers choices (`edit_choices`). The reply states what changed, what only Power BI shows, what was kept as it was, and offers Undo. A measure change is never applied without a confirmation card (`edit_confirm`). "What if the target were 120K" stays a what-if (answered by the local DAX engine, no model); "change the target to 120K" is an edit that asks first. The events, routes and rules are in `imported-report-editing-api.md`.
9. **UI** (a new imported editor, `ImportedEditor.tsx`, not grown into App.tsx):
   - canvas selection (click, Ctrl-click, Alt to reach the visual underneath) with drag, eight resize handles, arrow-key nudging, delete, duplicate, align and distribute, and a keyboard list of every visual with show/hide and stacking order;
   - a Format pane from the server's format catalog (curated sections first, "All properties" searchable) with a color picker offering theme colors 0..9 with tints, hex and recent colors, and a "Shown in Power BI" tag on what the canvas does not draw;
   - a Fields pane (wells by role, add, replace, remove, visual type, sort, an Add visual gallery), and page tabs with rename, add, duplicate, delete, reorder (drag or buttons), hide, size and background;
   - a theme editor and a measure editor (edit, rename, delete, new) whose changes wait in "Pending Power BI changes" until the user applies them in one Desktop cycle, and DAX Lab's "Save as a measure" for imported reports, which queues there too;
   - a History pane (undo, redo, "Restore to before this" with the list of what it would leave out);
   - the delivery bar: "Edited in Studio · N changes not in your file", "Save to file…", "Save a copy…", and "Open the edited copy in Power BI";
   - receipts, questions and confirmations from chat as cards under the conversation (with the receipts of earlier turns read back after a reload).
10. **Renderer fidelity** covers custom themes, ThemeDataColor mapping, default series colors, fonts, data labels, legend, borders and shadows, transparency, z-order and isHidden, so most edits show instantly; a change that only alters how things look re-reads formatting without running any DAX. Every visual carries a `fidelity` (`exact`, `approximate`, `none`) and the list of what is set but not drawn, and each edit's receipt says `visible_in` (`studio`, `powerbi_only`, `covered_by_background`). A Desktop screenshot is the full-fidelity check.

## Validation gates

- **Structural validation** of every revision (`report_edit_validation`, run inside the batch before anything is committed): unique page and visual names, `pageOrder` and `activePageName` consistency, `$schema` present with the required keys, positions finite and inside the page (page coordinates, group origins added), selectors, bindings, sorts, filters, interactions, bookmarks, group parents and page links that resolve, filter names unique report-wide, literal types that match the catalog (only for literals this batch wrote), and filter objects that satisfy Microsoft's filterConfiguration 1.3.0 and semanticQuery 1.4.0 schemas (packaged in `pbir_schemas/`, checked with `jsonschema`).
- **Only problems the edit introduced block it.** `capture_baseline` records the head's own findings before the batch runs; a copied visual or page inherits its source's baseline (`lineage`). Advisory findings (overlap, tiny size, tab order, visual types Studio cannot draw) never block; they are shown.
- **Survival check:** after Desktop saves a Studio-written file, `pbix_diff` verifies that every property, part and resource Studio wrote is still there (only float text, key order, `$schema` bumps and `SecurityBindings` may differ); anything else fails the batch and names the dropped edit.
- **The Microsoft `powerbi-report-author validate` gate of the first design was not built**, because its findings on a Desktop-authored report are noisy (the untouched Agents report already has 5). The structural checks above, the two Microsoft schemas and the Desktop contract script replace it.
- **Model operations:** DAX is checked structurally before Desktop (names, the references that can be resolved offline; the local DAX engine is deliberately never asked to evaluate a measure, review P36), then applied in Desktop, checked by Desktop's own error state and probed with `EVALUATE`. A failure is rolled back with the inverse TOM write, never by closing without saving.

## Scope and refusals

In scope:
- PBIR-in-PBIX reports.

Refused, with an explanation:
- Legacy `Report/Layout` files: `capabilities.edit_report` is false, with the steps to upgrade the file in Desktop.
- Thin, live-connection reports: report-layer edits are allowed (with a persistent notice that Studio cannot preview their data), model edits are refused.
- Encrypted or sensitivity-labelled files.
- Anything in the capability matrix (`edit_capabilities.py`): the reply names the capability and gives the Desktop steps, and no operation is attempted.

## Delivery

- Studio never touches the user's source file silently: "Save a copy…" is the default, "Save to file…" is an explicit, confirmed step.
- "Save a copy…" writes the head revision to a chosen `.pbix` path (default `<name> (edited).pbix` next to the source). It is created exclusively, so a taken name gets ` (2)`, ` (3)` and nothing is overwritten.
- "Save to file…" (replace original) names the file and the backup in a confirmation, refuses when the user's own file changed after the import (or after Studio last wrote it) or is open in Desktop, makes `<name>.pbix.studio-backup-<UTC time>` first, and swaps the file in atomically.
- A Studio-written file has no `SecurityBindings` until Desktop saves it once; that is normal.

## Not verified yet

Everything above that touches a browser, the packaged app or Power BI Desktop was tested with fakes, server-side rendering and pure-function tests, because the agents that built it were not allowed to start them. A human still has to check, on the real Agents dashboard: drag, resize and keyboard nudging on the canvas; the dialogs and focus behavior; a full model-edit cycle (create, rename, delete a measure) on Desktop 2.157; the packaged build (`scripts/build-desktop-sidecar.ps1`), including the filter-schema check; and the model receipts folder `evals/imported_edit/evidence/`, which does not exist until the contract check has been run and committed (until then `capabilities.edit_model` is false in the packaged app). Also unprobed in Desktop: a fixed-decimal filter column, hierarchy levels, axis start and end literals, the text box paragraph shape, and the shape and text box objects Studio writes.

## Measured decisions (2026-09-28, Desktop 2.157.1354.0; these override anything above)

**ThemeDataColor (exp5).** A probe page of `shape` tiles filled with `ThemeDataColor` ColorId 0..41 at Percent 0, -0.4
and +0.4 under a custom theme whose background, foreground and 42 data colors were all distinct, sampled from a bridge
screenshot (the screenshot scales every color by 213/255 uniformly, literal-hex calibration tiles show the same factor):

| ColorId | Resolves to |
|---|---|
| 0 | `#FFFFFF`, **not** the theme's `background` (the theme had `#203040`) |
| 1 | `#000000`, **not** the theme's `foreground` (the theme had `#F0E0D0`) |
| 2..9 | `dataColors[0..7]` (`dataColors[n-2]`), verified for all eight |
| 10 and up | invalid: Desktop falls back to the default fill (`dataColors[0]`) |

The palette is therefore `["#FFFFFF", "#000000", *dataColors[:8]]`, and Studio may write ColorId 0..9 only. Percent is applied
as Power BI's shadeColor with half-up rounding: Percent < 0: `floor(c * (1 + p) + 0.5)`; Percent > 0:
`floor(c + (255 - c) * p + 0.5)`. All 30 sampled tiles matched to within 1 RGB level. Probe: `evals/imported_edit`
`desktop_contract.py` step 8 repeats this. (An earlier version of this design said ColorId 0 is the theme background and 1 the
foreground; that was inferred from the grey "Others" bars under a white theme and is wrong.)

**Instance selectors are mandatory for some visual types.** `instance-selectors.json` in the schema package lists visuals whose
objects only take effect under a state selector: `shape` (`fill`, `outline`, `shape` -> `{"id":"default"}`), `cardVisual`,
`actionButton` (default/hover/selected/disabled), `pageNavigator`, `bookmarkNavigator`, `filterCard`, `pivotTable`. A `shape`
whose fill was written **without** `"selector": {"id": "default"}` drew in the default fill (`dataColors[0]`) in Desktop, while
the same entry with the selector drew the requested color. Every writer must put such properties under the default selector.

**Visual groups (exp6).** A hand-built PBIR group (`visual.json` with `"visualGroup": {"displayName", "groupMode":
"ScaleMode"}` and `position`, children carrying `"parentGroupName": "<group name>"`) opened in Desktop, rendered, and survived
a Desktop save unchanged. **Child `position.x/y` are relative to the group's own origin**, not the page: a child stored at
(100, 100) in a group at (300, 300) drew at page (400, 400); a second child at (250, 150) drew at (550, 450). So a child's page
position is `group.x + child.x`, `group.y + child.y` (nested groups add up). Moving a group therefore needs no change to its
children; hit-testing, bounds checks and "place next to" must add the group origin. Resizing a group in `ScaleMode` scales its
children in Desktop; Studio's first version refuses to resize a group (moving is allowed).

**Filters (exp7/exp8).** 30 filters written by `report_filters.build_filter` on scratch pages of table visuals (visual and
page scope) were opened in Desktop, screenshotted, and Desktop's saved file compared with what Studio wrote. Desktop returned the
expected rows for every one, and its saved `filterConfig` was identical (parsed JSON) to Studio's for all 30: categorical in and
not-in on a text and a whole-number column (with a `(Blank)` entry), advanced comparison on a column, a datetime column
(`datetime'...'` literal) and a measure, is / is not, AND / OR of two conditions, contains, starts with, not contains, not starts
with, is blank / is not blank, an aggregated column (`Sum of SalesAmount`), Top N and Bottom N by a measure, relative date last N
years / months / weeks / days and last N calendar years / months (the `TimeUnit` codes are right: years are unit 3, not the
decade), a locked and hidden filter (flags survive a save), and a page-scope filter. Probes: `exp7_build.py`, `exp8_build.py` in
the session scratchpad. All `VERIFIED_VARIANTS` are therefore true; a fixed-decimal (`M`) column and a HierarchyLevel field were not
probed (the test model has neither).

**Opening, saving and the contract (2026-09-29).** `evals/imported_edit/desktop_contract.py` passed all ten checks on Desktop
2.157.1354.0; its receipt is `evals/imported_edit/evidence/desktop-2.157.1354.0.json` and is what turns measure edits on. Findings
that changed the code:
- Desktop marks **even the untouched original** as changed while it loads, not only a Studio-written file.
- A Save pressed while the report is still loading is ignored without a trace, and a Save made then can be followed by Desktop
  marking the report changed again (the original: file written 30 s after launch, flag set again). `DesktopController.save` presses
  Save again after 12 s of no progress while the report is still unsaved (at most 4 presses). Open-and-normalize now takes ~40 s and
  the report stays clean afterwards.
- The save Studio makes right after opening rewrites the working copy. `ReportEditor._open_placed` records those bytes as the placed
  copy of the same revision (`RevisionStore.replace_placed_copy`); before that, every reopen produced a spurious "Changes saved in
  Power BI Desktop" step on the next edit, which also broke Undo.
- The "Save changes?" prompt on close is not reachable by UI Automation (`dont_save_reachable: false`); Studio still never closes a
  dirty Desktop. An external TOM rename does not make Desktop rewrite report references (`rename_rewrites_report_references: false`),
  so Studio rewrites them itself.

**Live battery.** `evals/imported_edit/live_battery.py` drives the HTTP API against a running sidecar (isolated state:
`studio-sidecar-edit-qa` in `.claude/launch.json`). On 2026-09-29 against the Agents dashboard all 12 steps passed: import, render
with revision echo, recolor + undo, conditional formatting protected until confirmed, move / add in free space / delete, a page filter
that changes 12 visuals and clears again, theme with the overrides question, history + restore to the original, save a copy twice,
and, through Power BI Desktop, open the edited copy, create a measure, refuse to delete a used one, rename, and undo twice back to
the import with a clean history.
