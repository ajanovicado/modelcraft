# Imported-report editing: design review decisions (2026-09-28)

Five independent reviewers (PBIR fidelity, safety, UX/model, renderer/preview, testing/scope) attacked
`docs/imported-report-editing.md`. This file is the adjudicated result. **Where it conflicts with the
design document, this file wins.** Each item is a required change; `P<n>` is its priority.
The styling-edit decisions are historical: the user removed that ability on 2026-09-30,
and removed the new-draft theme picker on 2026-10-02. The API and user guide describe the
current supported edits; renderer fidelity decisions continue to apply.

Amendments that apply everywhere:

- Desktop is identified by a pid snapshot + `create_time` + `currentFilePath`, never by the pid `open` prints (P1).
- The `hasUnsavedChanges` flag is not evidence of user work (Desktop dirties a Studio-written file by itself ~10 s after load). Studio never saves the user's Desktop work silently, never kills Desktop, and never closes it while a dialog or unsaved state is present (P2, P4).
- A batch that contains model operations is one transaction: model first on the current head, then report ops on the saved result, then ONE head move; on failure the head does not move and Desktop is rolled back with compensating TOM ops (P5).
- `RevisionStore` (`report_revisions.py`) owns history, placement (`placed`) and adoption. No one else reads `archived_path` for the current report state (P3, P19).

## Required changes

### P1

Section 7 desktop_session ('bridge open ... recording the PID'). Never use the pid that `powerbi-desktop open` prints. Before launch, snapshot the PBIDesktop.exe pids. After `open`, poll `status` until one instance matches all of: currentFilePath resolves to reports/<id>/desktop/Report.pbix, its pid is not in the snapshot, and bridgeStatus is connected. Persist (pid, psutil create_time, path) in SQLite. Before every Save, close, screenshot or TOM call, re-check through `status` and psutil that the pid, create_time and currentFilePath are unchanged. desktop_uia calls use only that pid, and Studio refuses on a mismatch or when two instances hold the same path.

*Why:* Verified in hermes/node/.../powerbi-desktop-bridge-cli/dist/index.js. Lines 259-296: findBridge() without a pid returns the only connected instance. Lines 467-486: `open` calls findBridge with no pid and prints that instance's pid; its own pidNote says to use `status` instead. Failure: the user has Budget.pbix open in their own Desktop. Studio's `open` returns before its new instance connects, so the UIA Save and the close hit Budget.pbix and save or close the user's half-finished work. With two instances connected, `open` returns no pid at all. The scratch contract.py already uses the snapshot approach; the design text still records the pid from `open`.

*Raised by:* safety

### P2

Section 7 mixed batch, steps 1-2. Reorder so that adoption follows the save. (1) If the tracked Desktop has user work (see priority 4), ask the user first, and never save on their behalf silently: 'Power BI Desktop has unsaved changes: Save them into history and continue / Apply when I close Desktop / Cancel', with Cancel as the default. (2) UIA Save, then wait until all of these hold: the flag is false, the hash has changed, zipfile.testzip() is clean and read_report parses the file. (3) Close and wait for the pid to exit (psutil, create_time checked). (4) Only then hash the working copy and adopt it against placed_hash (priority 3). (5) Build the Studio revision on the adopted head. Re-check the adoption precondition immediately before any write to the working copy. The chat reply names the adopted Desktop revision separately from Studio's change.

*Why:* As written, step 1 checks for a user save before step 2 performs the save. The bytes saved in step 2 are never adopted, and step 5 overwrites them with head. Failure: the user moves three visuals in Desktop without saving and asks chat to 'make the bars green'. Step 1 sees working == head, step 2 saves the moves, step 3 builds on the old head, and step 5 copies head over the working copy. The moves are gone, and no revision holds them. A silent save also makes a user's throwaway experiment (a deleted page) permanent.

*Raised by:* safety, ux_model

### P3

Architecture ('desktop/Report.pbix = copy of head'), section 6 Undo, section 7 steps 1 and 5. Persist placed_revision and placed_hash: the bytes Studio last materialized into the working copy. A user save means hash(working) != placed_hash, not != head. Every head move (edit, adopt, undo, restore) materializes head into the working copy inside the same locked batch when Desktop is closed. When Desktop is open, set working_stale and materialize before any open. 'Open in Power BI' runs adopt-then-materialize under the edit lock; this replaces service.py `if not working.exists(): shutil.copyfile(...)`. A user save detected while head != placed_revision becomes a revision whose parent is placed_revision. It is shown as a conflict, resolved by a per-part three-way merge of Report/** (base = placed, ours = head, theirs = user save) or by an explicit choice, and is never stacked on head. Migration: existing reports get head = r0000 = original, and an existing working copy whose hash differs is adopted, never overwritten.

*Why:* Verified: service.py:499-500 copies the working copy only when it is missing, so it is never refreshed. Failure: Desktop is closed and chat recolors, so head moves to r0005 while the working copy still holds r0004. The next batch sees working != head and adopts r0004 as r0006, which silently reverts the recolor. 'Open in Power BI' also opens the pre-edit bytes, and the user's later save of them is stacked on the newer head.

*Raised by:* safety, testing_scope

### P4

Section 7 ('never kills a Desktop with unsaved user work'), the Evidence row 'pbi measure create sets hasUnsavedChanges', and Validation gates ('closing without saving and reopening'). Define the Desktop close protocol:
(a) After every Studio-initiated open, wait for Desktop's own dirtying, UIA-save, and record placed_hash. Create no revision when semantic_diff (priority 7) against the source revision is empty.
(b) 'User work' is a semantic diff, never the flag.
(c) Before Save or close, list the pid's top-level windows. desktop_uia.windows already returns the enabled state. If any modal is present or the main window is disabled, abort, leave Desktop running, and report the dialog title.
(d) Verify a 'Don't save' path by AutomationId in the contract run. Until it is verified, never close a dirty Desktop.
(e) Delete every kill fallback (desktop.ps1 Stop-Process -Force).
(f) Roll back failed model ops with compensating TOM ops (a before-image from `pbi measure get`) followed by a normal save, not with close-without-save.
(g) Correct the evidence table: the flag flips on its own, and the UIA save measured 5.5 s.

*Why:* Verified in scratchpad editexp/contract-log.json (13:22, Studio-written smoke.pbix): dirty_at_connect false, dirty_after_10s true with no action taken, dirty_after_save false, dirty_after_tom true. After WM_CLOSE the main window reports enabled:false, which means the Save-changes modal is up. dialog2.ps1 was written to inspect that dialog, but no result was recorded. exp4-log.json D8_close reads '20.7s killed 5832'. So the flag cannot tell user work from Desktop's own state, and the only measured close of a dirty Desktop ended in a kill. A kill loses any real user edits and leaves AutoRecovery state behind.

*Raised by:* safety, testing_scope, ux_model

### P5

Section 7 steps 3-4, Validation gates ('rolled back by not saving'), and section 5 CreateMeasure/RenameMeasure. Make a batch that contains model ops one staged transaction. The step-3 output goes to reports/<id>/staging and is not a revision. The order is:
1. Open the current head, whose names are consistent.
2. Apply the model ops through TOM.
3. Check the TOM State/ErrorMessage and run EVALUATE.
4. UIA save.
5. Run the survival diff (priority 7).
6. Apply the report ops, including a rename's reference rewrite, to the saved file.
7. Commit ONE revision that holds both layers, and move head once.
On any failure: compensating TOM ops, delete the staging file, head unchanged. ModelInfo is updated only from pbix_extract of the committed file: remove the optimistic `model.measures[...] =` writes in _check_create_measure and _check_rename_measure. Before commit, every Measure and Column reference must resolve against the saved model. The reply says 'partial' or 'failed', never 'updated'.

*Why:* Verified: apply_operations marks RenameMeasure as report_changed, and the _check_* handlers mutate ModelInfo before Desktop runs. Failure: 'rename Target to Goal' writes a revision whose visuals bind [Goal] and moves head. The Desktop open then hits a sign-in modal, or the probe fails. Head now binds visuals to a measure its DataModel lacks: the local engine reports an unknown measure, Desktop shows 'Fix this' tiles, and only Undo recovers. Likewise, 'create Avg Discount % and show it in a card' leaves a card bound to a missing field when the DAX fails.

*Raised by:* pbir_fidelity, safety, testing_scope

### P6

Section 8 ('re-renders') and section 10 ('most edits show instantly'). The imported-report payload carries head {revision_id, hash}. InteractiveReportPage receives it as a prop and adds it to the effect deps and to the in-flight dedupe key (path + body + head). The request sends ?revision=<id>. The server returns 409 for a non-head revision and echoes `revision` in every payload, and the UI drops responses whose revision is not the current head. Move and Resize update the layout optimistically while the revision is written.

*Why:* Verified in InteractiveReportPage.tsx: the pending key is path + body (lines 28-44), and the effect deps are [active, path, body, revision], where `revision` is local manual-refresh state (line 329). After 'make the TopN bars green' or an Undo, the canvas keeps the old colors until the user clicks 'Refresh visuals'. A render for the old head that is still in flight is handed to the next effect, so even an automatic refetch shows stale colors.

*Raised by:* renderer_preview

### P7

Section 2 ('compact JSON ... what Desktop writes') and sections 6/7 (adoption, History). Define one semantic_diff(rev_a, rev_b):
- Report/** parts compared as parsed JSON, independent of key order and float text.
- Untouched parts and DataModel compared as bytes.
- Model metadata compared through pbix_extract: measures, format strings, relationships and row counts.
Use it for no-op and adoption detection and for History's 'what changed'. After every Desktop save of a Studio-written file, also use it as a survival check: every property, part and resource Studio wrote must still be present. Only whitelisted normalizations are allowed: float text, key order, whitespace, $schema bumps and SecurityBindings. Any other difference fails the batch, keeps head, and names the dropped edit.

*Why:* Verified: json.loads followed by a compact json.dumps reproduces only 14 of 19 Report/definition JSON parts of the original byte for byte. Desktop writes 17-digit doubles (966.14173228346453, where Python writes 966.1417322834645). Hash-based adoption would therefore create a 'desktop' revision after every save, and History would list visuals nobody moved. Desktop's save in exp1 garbage-collected the background PNG, its RegisteredResources item and the png Default (report.json 1217 to 1040 bytes). A resource Studio registers in a shape Desktop considers unreferenced would vanish while the reply says 'applied'.

*Raised by:* testing_scope, safety

### P8

Sections 6-9, which specify no concurrency control, and section 8 (chat runs inside the stream). Add a per-report blocking FileLock, reports/<id>/edit.lock (timeout, queued), held for the whole batch including the Desktop lifecycle. open_imported_report, previews, Desktop DAX queries and delivery take it too, in the order edit.lock then native-preview.lock. Every mutation carries expected_head. The server re-applies the typed ops to the current head when their targets still exist and returns 409 otherwise. Revision ids are allocated inside the SQLite transaction. The idempotency key is inserted as 'pending' when the batch starts, and duplicates get an in-progress answer. Relative ops ('+2', 'darker') are resolved to absolute values when parsed. Batches run in a per-report serial worker that is not tied to the HTTP request. Chat and the Format pane only enqueue a batch and stream its progress, and clients re-attach by batch id. Cancel is honoured only before Desktop is touched. The outcome is recorded in chat_actions when the batch finishes.

*Why:* Verified: native_report uses a global native-preview.lock with timeout=0. The optimistic check in state.py uses base_hash, which equals source_hash forever. api.py closes the chat generator on disconnect. Failures: a canvas drag and a chat recolor both read r7, and one edit is lost. A workspace switch during 'Closing Power BI Desktop…' aborts the batch between steps 2 and 3, leaving Desktop closed and the user's saved work unadopted. A UI retry of 'font size +2' applies it twice.

*Raised by:* safety

### P9

Sections 6/7 (crash consistency), which are unspecified. Add an edit_batches journal with the states planned → desktop_saved → desktop_closed → staged(hash) → committed → placed(hash) → model_applied → model_saved → adopted → reopened. Each state is written before its side effect, and every step is idempotent. On sidecar start, resume or roll back open batches, delete orphan staging files, and re-adopt Desktop instances whose currentFilePath is under reports/*/desktop/ (pid and create_time). Launch Desktop outside the sidecar's kill-on-close job, using CREATE_BREAKAWAY_FROM_JOB after checking JOB_OBJECT_LIMIT_BREAKAWAY_OK, or AppX activation. Add a test that kills the sidecar tree and asserts that Desktop is still alive, in both dev and the packaged build.

*Why:* Verified: apps/desktop/src-tauri/src/lib.rs:56-60 calls child.kill() on window destroy, and the bridge spawns Desktop detached but without breakaway. The memory note records that a sidecar restart closed Desktop (2026-09-27). Failure: a reseal mid-batch after a TOM CreateMeasure kills Desktop, and the measure is lost with no record. A crash after the revision row but before materialization leaves head != placed, which feeds the silent revert in priority 3.

*Raised by:* safety

### P10

Delivery. For 'Replace original':
- Re-hash the source and require it to equal source_hash or the last delivered hash; otherwise refuse and offer Save a copy.
- Refuse if any bridge instance has the source open.
- Write a temp file in the same folder and call ReplaceFileW with a backup (<name>.pbix.studio-backup-<ts>).
For 'Save a copy': exclusive create, adding a suffix instead of overwriting.
Both deliveries run adoption first, warn when the tracked Desktop is dirty, and run the integrity checks (testzip, read_report, pbix_extract) on head. The user is told that a Studio-written head has no SecurityBindings until Desktop saves it. The toolbar shows a persistent 'Edited in Studio · N changes not in your file · Save to file… / Save a copy…' status, and 'Open in Power BI' is labelled 'opens Studio's edited copy'.

*Why:* Failures: the user kept editing the Downloads file in their own Desktop, then confirmed Replace, and a week of their changes is overwritten. 'Save a copy' silently replaces an earlier '(edited).pbix'. Users open their original, see none of the edits and conclude Studio saved nothing; the App.tsx tooltip still says the imported report is preserved. report_import already records source_hash, so the check costs nothing.

*Raised by:* safety, ux_model, testing_scope

### P11

Section 3 selectors, section 5 SetColors/SetFont/SetVisualProperty, section 10. Specify effective-value precedence and protect conditional formatting. pbir_expr gets a resolver: for a target instance, collect the entries that cover it and rank them data+metadata > data (scopeId or wildcard) > metadata > id:'default' > none. Confirm the ranking once with a Desktop diff. Document the existing set_everywhere as the 'whole visual' write, with three rules. (a) It never overwrites a non-Literal value (a Measure, FillRule or Conditional expression) without an explicit confirmation that names what is lost. (b) When narrower overrides exist, the reply or chips state what gets replaced ('replaces TopN blue, BottomN red, Others grey'), or offer 'only TopN'. (c) report_formatting.properties merges by the same specificity instead of list order. An op that changes no visible mark fails validation.

*Why:* Verified in code: set_everywhere writes into every narrower entry that already sets the property. 'All bars green' does repaint TopN, BottomN and Others, so the review claim that it 'paints nothing' is outdated. But 'make the table text navy' now overwrites e82e02f9's four values.fontColor Measure expressions ('CF Table', 'Rank Filtering Employyees ... CF') with a literal, which destroys the conditional formatting silently. report_formatting.properties (lines 33-42) applies result.update in list order. On fb4c5aa3 ([{metadata}, {id:'default'}]), a metadata-scoped edit therefore shows in Desktop but not in Studio.

*Raised by:* pbir_fidelity, ux_model, renderer_preview, testing_scope

### P12

Section 3, the ThemeDataColor mapping ('confirmed by the grey Others bars'), and section 10. Before building the color picker or the theme renderer, run exp5 on a scratch copy. Use a custom theme with distinct background, foreground, backgroundLight and foregroundNeutralSecondary colors and 12 data colors. Fill chart categories with ColorId 0, 1, 2, 3, 9, 10 and 11 at Percent 0 and ±0.4, take a bridge screenshot, and sample the pixels. Keep the verified table in one function, pbir_expr.theme_palette, and have report_formatting.color call it. Until then, theme_color accepts only verified ids and the Format pane writes hex. Match Desktop's shadeColor rounding with floor(x+0.5): Python's round(178.5) gives 178.

*Why:* Verified: the file contains only ColorId 0 (13 uses) and ColorId 1 (8 uses). The grey Others bars only show that ColorId 0 resolves to white under a #FFFFFF background. The renderer lens's hue sampling on the exp1, exp3 and exp4 screenshots found ColorId 1 text neutral black (mean R-B 0.00), while text in the theme foreground #252423 measured R-B 1.2. That suggests ColorId 1 is not the theme foreground. Failure: with a dark custom theme, Studio repaints the Others bars and axis labels inverted relative to Desktop, and a picker 'theme color 1' writes ColorId 2, which has never been observed.

*Raised by:* pbir_fidelity, renderer_preview

### P13

Section 3 literals ('D/L suffixes'), section 4 catalog, and the Validation gate 'literal types match the catalog'. Take literal types from the model and from Desktop, not from guesses.
(a) snapshot_formatting_catalog.py keeps the 'formatting' subtypes, mapped explicitly: fontSize and labelDisplayUnits → D; fontFamily, alignment, formatString and altText → string. An overwrite reuses the suffix Desktop already wrote. D and L are interchangeable on read. The literal gate checks only literals Studio wrote.
(b) Data literals in category selectors and filters are typed from the model. Extract Column Explicit/InferredDataType, Measure.DataType and SummarizeBy through the fixed-SQL adapter, and map 2→'text', 6→L, 8→D, 10→M, 9→datetime'…', 11→bool. What-if and numeric-range slicers use D. Chat values are coerced to the type, and unconvertible values are refused. Verify M and datetime with a Desktop diff.
(c) A font table captured from Desktop maps each display name to Desktop's CSS-stack literal, and unknown fonts are refused.

*Why:* Verified: categoryAxis/valueAxis.labelDisplayUnits has type 'formatting' in pbir_catalog.json, so encode_property writes "'1000'" as a string. labels.labelDisplayUnits, the one SetDataLabels writes, is an enum and is written correctly as D. advancedSlicerVisual accentBar.width is 'numeric' in the catalog, but Desktop wrote '6L', so a report-wide gate would block every edit to 29ba010b. literal_value (pbir_expr.py:101-116) types from the Python value: 'color the 2019 bar red' writes "'2019'", which never matches an Int64 axis, and there is no M suffix for Decimal columns. All 14 fontFamily literals in the file are stacks like "'''Segoe UI Bold'', wf_segoe-ui_bold, ...'".

*Raised by:* pbir_fidelity, testing_scope

### P14

Section 3 selectors ('default, metadata queryRef, dataViewWildcard, scopeId'). Model a selector as an open record {data?, metadata?, id?, order?} that is preserved verbatim. State ids such as 'selection:selected' and reference-label ids such as 'field-<uuid>' come from the visual's existing entries, plus a small Desktop-verified table for new ones. Ids are never validated against instance-selectors.json. The ops expose 'state' and 'reference label' targets.

*Why:* 53 id selectors in this file are not in instance-selectors.json: advancedSlicerVisual fillCustom/value/outline {id:'selection:selected'}, and cardVisual referenceLabel {data, id:'field-…', metadata, order}. Failures: 'make the selected tile dark blue' writes the catalog's 'selected' on 'fill' and changes nothing. 'Make the Target: label green' can only write the metadata scope, which recolors both reference labels on 2202e311.

*Raised by:* pbir_fidelity

### P15

Section 5 SetTitle, section 8 target resolution, section 10. Rename SetTitle to SetCaption, backed by one per-type caption map shared by the writer, the import index and the renderer:
- cardVisual → objects.label[metadata].text
- slicer → header.text
- legacy card and tableEx columns → projection displayName
- textbox → paragraphs
- charts → the container title
report_import._visual stores report_edits.visible_caption, which already exists, and decodes literals with pbir_expr.decode instead of strip("'"). Expression-bound titles are stored as '(dynamic title)'. Studio detects caption overlays, a text-only visual lying over another (8a8ba163 over 953a59a3), and offers both as targets. The renderer draws the container title row for cards too.

*Why:* Partly implemented: _CAPTIONS already routes cardVisual to label and slicer to header. The index still uses the container title or the first binding, so card 2202e311 is indexed as 'Employees Avg MTD Sales' while it shows 'Agents Avg MTD Sales ', and both channel slicers index as 'ChannelName' although 54be5cf0 shows 'Country'. As a result, 'rename the Agents Avg MTD Sales card' does not resolve. report_import._literal leaves doubled quotes in place, so SetCaption("Agent's Sales") is displayed as Agent''s Sales.

*Raised by:* pbir_fidelity, ux_model, renderer_preview

### P16

Section 5 SetTheme, section 8 'theme' and recolor intents. Add a per-report color inventory, built at import and per revision. It maps each color to its layer (visual literal, ThemeDataColor, a DAX measure literal including the SVG measures and 'green'/'red', or the page image), with its usages and hue family. Add ReplaceColor{from, to, scope}:
- It rewrites literal and ThemeDataColor colors in place.
- It turns DAX-embedded colors into pending UpdateMeasure edits.
- It states that colors baked into the image cannot be recolored.
SetTheme first computes the override inventory (visual properties pinning a fill or fontFamily). It offers 'apply theme and clear N overrides' or 'rewrite N overrides', and the reply states how many visuals visibly change. Brand-palette requests map to ReplaceColor when colors are hard-coded. For a SharedResources customTheme, copy it into a new RegisteredResources CustomTheme item and repoint themeCollection.

*Why:* On the Agents dashboard every chart has per-category fills, the table uses conditional-format measures, 14 fontFamily literals pin fonts, the card bars are hex colors inside 4 SVG measures, and the header and panels are pixels in an opaque 8000x5000 PNG. exp3's theme had no visible effect. 'Use Arial everywhere' or 'make the palette green' would be reported as done with nothing visibly changed. The former theme handler also split resource paths at `RegisteredResources/`, which returned a whole SharedResources path and wrote a stray part.

*Raised by:* pbir_fidelity, ux_model, testing_scope

### P17

Section 10 renderer fidelity: the renderer must read everything the ops write.
- Deep-merge the base and custom themes, merging textClasses and visualStyles per key and replacing arrays; pbir_document.theme uses dict.update.
- Provide result.theme.dataColors through ReportPaletteContext instead of the Studio draft palette.
- Emit defaultColor (the selector-less dataPoint fill), seriesColors (metadata selectors) and categoryColors (also decoding datetime literals), and delete the hard-coded '#477fbc'.
- Emit title, label, legend and axis tokens with textClasses defaults.
- Resolve the container (background plus transparency, border, radius, shadow, padding) from visualContainerObjects, then the theme's visualStyles.
- ImportedVisual gains z, tabOrder, hidden and parentGroup: sort and zIndex by rank with isolation:isolate, and draw hidden visuals as ghosts in edit mode.
- Draw visualGroup with no box.
- Tell clustered types from stacked ones.
- Apply page background transparency as alpha.

*Why:* Verified: InteractiveReportPage.tsx:126 and 134 hard-code #477fbc, and report_import._visual reads only x, y, width and height. exp4's chart without explicit colors is #00A651 (the theme's dataColors[0]) in Desktop and #477fbc in Studio. A Desktop save reorders the visual folders alphabetically, so the 8a8b caption and the 953a slicer swap paint and hit order. Without these tokens most formatting ops write valid PBIR that Studio's canvas never shows.

*Raised by:* renderer_preview

### P18

Section 8 ('reply states what changed'), section 9 'Preview in Power BI', section 10 ('A Desktop screenshot is the full-fidelity check'). Each op returns visible_in: studio, powerbi_only or covered_by_background. visual_format returns a fidelity of exact, approximate or none, plus the property paths that are set but not drawn; the catalog tags each property studio_render. Receipts say 'Visible in Power BI only · Check in Power BI'. At import, detect opaque page images by alpha coverage. Background and header requests then get Remove image / Replace image / Keep, and text inside the image is never mapped to another target. Previews are stored at previews/<head_hash16>/<page_id>.png and captured only when Desktop has the head placed and saved with no unsaved changes. Desktop is never auto-launched for a formatting op.

*Why:* Verified: native_report.py:99-102 caches previews/page-{index}.png next to archived_path and reuses it. After a recolor or an Undo, 'Preview in Power BI' returns the pre-edit PNG, and after ReorderPages it returns another page's image. 'Set the page background to white' is covered by the opaque PNG in both Studio and Desktop, yet the reply would say it changed.

*Raised by:* renderer_preview, ux_model, safety

### P19

Section 6 (re-derivation, 'every reader switches to head'). Store per-revision derived metadata: pages, measures with expression and format string, the schema, row counts, the snapshot id, and model_fingerprint = sha256 of that canonical metadata. Every head move switches ImportedReport to the target revision's metadata in one transaction. The snapshot is re-decoded only when row counts or the schema change, and old snapshots are kept for Undo. Key report_engine, page_style and previews on the head hash or the model fingerprint, not on base_hash. Key model_formats on the DataModel CRC32 and size from the zip central directory, which stay identical across Studio report-layer revisions. Add a formatting-only render path (visual_format with no DAX) for formatting ops, and a lint test that only report_revisions reads archived_path.

*Why:* Verified: report_engine.py:51 keys the cache on (id, source_hash, base_hash, snapshot.id), and base_hash never changes for imported reports. Failures: after undoing CreateMeasure 'Margin %', DAX Lab and the binding validator still accept it, so an AddVisual on [Margin %] is saved against a model that lacks it. After a refresh and save in Desktop, chat still answers from the import-time snapshot. Every formatting tweak also pays for an engine rebuild, a pbixray subprocess (about 0.9 s) and a full-page DAX query.

*Raised by:* safety, renderer_preview, testing_scope, ux_model

### P20

Section 6 ('whole files', 'pruned beyond 30') and section 1 ('writes atomically'). Store revisions as content-addressed blobs, revisions/<sha256>.pbix, referenced by revision rows. Pin head, placed_revision, the parents of in-flight batches, and original.pbix (kept outside revisions/). Retain by distinct content and a size budget. Prune in a best-effort pass after commit that retries locked files. Canvas gestures and Format-pane edits accumulate in the client and commit one revision when edits go idle (about 2 s) or on Apply; one chat message makes one revision. Writes go to a temp file in the destination folder, then fsync, then os.replace with a bounded retry on WinError 5 and 32, then a re-hash. A row never references a file that is not yet durable. Before touching Desktop, check free space: at least 3× the PBIX size on the state drive and 2× the DataModel size on %USERPROFILE%. Delete a report's folders when its workspace is deleted.

*Why:* Failures: an undo by pointer followed by pruning deletes the file head points at, and every render then fails. Ping-pong undo fills all 30 slots with duplicates. Pruning hits WinError 32 while live_report hashes the head, so the reply says 'failed' after head has moved, and the retry duplicates the edit. Twelve arrow-key nudges create twelve 4.8 MB files and push the chat recolor out of the undo window.

*Raised by:* safety, ux_model

### P21

Section 6 Undo, section 8 'offers Undo', section 9 History. Show Undo only on the newest revision (checked with expected_head). Older receipts offer 'Restore to before this', which lists the N later changes with their authors, including Desktop saves, and asks for confirmation. Alternatively, a per-part three-way revert is allowed when no later revision touched those parts. Undo and Restore run through the batch pipeline: lock, save, close, adopt, move head, re-derive, materialize. Add deterministic chat intents: undo, redo, 'undo the color change', 'restore original' and 'what did you change'. Add an SSE event edit_applied{revision_id, parent_id, ops, per-op summary, visible_in}, and toolbar Undo/Redo bound to revisions.

*Why:* As designed, Undo on an older reply moves head to that revision's parent. That silently discards later title changes and adopted Desktop work, and Desktop revisions carry no ops that could be replayed. 'Undo that' is not in the parser list, so it goes to the 4B model (25-90 s), which may emit an edit instead.

*Raised by:* ux_model, safety

### P22

Section 5 RenameMeasure ('rewrites every PBIR reference') and DeleteMeasure ('refused while a visual still uses the measure'). Build a reference_index covering every kind of reference:
- projections;
- object expressions (fontColor Measure, image.imageUrl SVG measures, referenceLabel.value);
- selector metadata;
- sortDefinition;
- filterConfig field and Where, resolving SourceRef.Source through the From alias list;
- page.json and report.json filters;
- bookmarks;
- DAX dependents, from the local DAX parser over measures and calculated columns and tables including NAMEOF, or INFO.CALCDEPENDENCY in Desktop;
- DAXQueries/*.dax (UTF-16LE).
DeleteMeasure is refused when any kind is non-empty, and the refusal lists the transitive visual users. For RenameMeasure, first test in the contract run whether Desktop fixes the PBIR itself on an external TOM rename made with the report open; if it does, adopt Desktop's save. Otherwise, rewrite Property (and nativeQueryRef when it equals the old name and there is no displayName) through the index, keeping queryRef stable as Desktop does. Add a golden test of the counts per kind.

*Why:* Verified: e82e02f9's filterConfig Where reads Measure{SourceRef:{Source:'#'}, Property:'Rank Filtering Dynamics'}, with From '#'→'# Measures'. _rename_references and _uses_measure match only SourceRef.Entity and walk only visuals, so a rename leaves the agent table's Top/Bottom-N filter pointing at a missing measure. 'Rank Filtering Dynamics' is IF(..., [Rank Filtering Employyees MTD], [Rank Filtering Employyees MTD%]), and no visual references either of those two measures directly. Deleting one therefore passes the current check and breaks the table's filter.

*Raised by:* pbir_fidelity, testing_scope

### P23

Section 5 Delete, Duplicate and the page ops, and Validation gates. Extend the reference index to three more kinds of names:
- visual names: visualInteractions, bookmark explorationState and targetVisualNames, parentGroupName;
- page names: visualLink navigationSection and drillthroughSection, visualTooltip.section, bookmark activeSection, pageBinding;
- filter names: bookmarks, boundFilter.
Duplicate mints new 'Filter' + 20-hex names and sets tabOrder to max+1000; AddVisual must stop deriving tabOrder from z. Delete removes the visual's interaction and bookmark entries and deletes groups recursively. Move and scale apply to a group's children with the group. DuplicatePage remaps visualInteractions and mints new filter and pageBinding identities. DeletePage refuses, or offers cleanup, and names the buttons, tooltips and bookmarks that reference the page. Structural validation checks all of these.

*Why:* Verified in report_edits.py: _apply_duplicate_visual deep-copies filterConfig names (PBIR_FILTER_NAME_DUPLICATE_GLOBAL) and keeps tabOrder. _apply_delete_visual removes only direct children, so a nested group leaves grandchildren with a dangling parentGroupName. pbir_document.remove_page fixes only pageOrder and activePageName, so deleting a tooltip page silently drops the custom tooltips on other pages. The Microsoft validator has no bookmark or interaction rules.

*Raised by:* pbir_fidelity, testing_scope

### P24

Section 4 catalog and section 5 AddVisual/ChangeVisualType. Replace catalog-order role placement with a hand-written per-type role table for every ADDABLE_TYPES entry. It gives the primary measure role, the primary grouping role, secondary roles and the small-multiples role, handles GroupingOrMeasure for scatter, and has a unit test per type. ChangeVisualType carries over every object and property that exists in the target type's catalog, keeps selectors whose queryRef survives the role remap, and lists what was dropped. It uses an explicit role map per type pair, and refuses or asks when a Category would land in cardVisual.Rows (small multiples).

*Why:* Verified: pbir_catalog.json roles are alphabetical because the snapshot uses sort_keys=True. kpi is [Goal, Indicator, TrendLine], so Total Sales lands in Goal and the value and target are swapped. gauge is [MaxValue, …, Y], so the measure lands in MaxValue and the op fails with 'needs Value'. Scatter's X and Y are GroupingOrMeasure and are never filled. _apply_change_visual_type pops every object (line 1219), so 'make Total MTD Sales a bar chart' loses the TopN/BottomN colors, the 16pt labels and the hidden gridlines, although barChart supports all of them.

*Raised by:* pbir_fidelity

### P25

Section 5 SetFields/SetSort, and the Validation gate 'bindings refer to model fields'. Match projections by field identity (Entity+Property, Aggregation(Column), HierarchyLevel) as well as by displayName and nativeQueryRef. Resolve sort items by field identity against the removed projections. Extract Column.SummarizeBy and use it, asking the user when it is None instead of silently writing Sum. Support HierarchyLevel bindings. Pass unknown field kinds (PropertyVariationSource, NativeVisualCalculation, field parameters) through untouched, and refuse to edit them.

*Why:* Verified: _apply_set_fields removes by nativeQueryRef only, so 'remove vs Previous Month from the table' fails, because that projection's nativeQueryRef is ' '. _drop_selectors compares Entity.Property with queryRef, so on fb4c5aa3 the sort on the removed measure survives: the sort's Property is 'Employees with Positive Change %' and the queryRef is '# Measures.Number of Employees with Positive Change %'. _field_projection defaults numeric columns to Sum, although the FactSales columns are SummarizeBy None.

*Raised by:* pbir_fidelity

### P26

Section 5 SetVisualFilter/SetReportFilter. Add SetPageFilter and FilterSpec variants encoded as Desktop writes them:
- Categorical: In with typed values.
- Advanced: Comparison/And/Or over a column or a measure.
- TopN: with Subquery.
- RelativeDate.
Each uses Version 2, From aliases, Source (not Entity) in Where, a unique 'Filter<20hex>' name, howCreated 'User' and optional lock/hide. Literal types come from priority 13. An existing filter on the same field is replaced, not duplicated. Each variant is exposed only after its Desktop golden matches. The renderer compiles page and report filters inside each visual's try block, so an unsupported filter marks those visuals 'needs Power BI' instead of failing the page, and Studio warns before writing such a filter.

*Why:* Verified: the ReportOperation union has no filter op. Validator codes: PBIR_FILTER_ENTITY_IN_WHERE, SOURCE_UNKNOWN, NAME_DUPLICATE_GLOBAL. pbi-cli already coerced '2014' to 2014L. live_report compiles global filters outside the per-visual try, and the resulting Unsupported is a ValueError that returns HTTP 422. So 'only show the top 10 agents on this page' works in Desktop but blanks Studio's whole canvas.

*Raised by:* pbir_fidelity, renderer_preview

### P27

Validation gates, Microsoft validator ('no new diagnostics'). Key diagnostics by (code, part path relative to definition/, JSON path). Duplicated visuals and pages inherit their source's baseline through lineage. Split codes into blocking (schema, ids, unknown object/property/literal type, bindings) and advisory (layout floors, overlap, bounds); advisory findings are shown to the user and never block. Ship pinned offline schemas: visualContainer 2.12.0, page 2.1.0, report 3.3.0, pagesMetadata 1.1.0, versionMetadata 1.0.0, and the theme schema. Validate them with jsonschema, which is already in uv.lock. When the Microsoft validator is installed, run it only with --no-schema, as an advisory extra. For an unknown $schema version, allow only catalog-known edits and warn. Studio's own gate also checks right and bottom bounds.

*Why:* A scratch run showed that duplicating slicer 54be5cf0 adds PBIR_SLICER_HEIGHT_BELOW_FLOOR (6 errors instead of 5), so the gate would refuse Duplicate and DuplicatePage. Messages embed current values ('70px < 76px'), so a diff of message text calls a resize a new error. Without --no-schema the validator fetches schemas from developer.microsoft.com, which contradicts PRIVACY.md ('no outbound network requests') and fails every revision offline.

*Raised by:* testing_scope, pbir_fidelity

### P28

The Evidence section and sections 3, 4 and 7 (Desktop version dependence). Commit evals/imported_edit/desktop_contract.py, grown from the scratch contract.py, writing JSON receipts under evidence/. A receipt holds:
- Desktop ProductVersion, the bridge manifest hash and the schema versions;
- the dirty flag at open, for the untouched original and for a Studio-written file;
- the Save-dialog AutomationIds;
- UIA save;
- TOM create → save → pbix_extract, including FormatString and row counts;
- rename behaviour and timings;
- screenshots.
Record the Desktop version in every revision. When the version changes, rerun the contract and keep model ops disabled until it passes; report edits stay allowed. Validate against the catalog only the properties an op writes, and pass unknown properties through. The design's evidence table cites receipt files.

*Why:* All the evidence lives in a session temp scratchpad. exp4-log.json records measure_persisted=false, and the newer contract-log.json shows the UIA save working but never checks that a TOM measure survives a save. Desktop updates monthly from the Store and cannot be pinned. A schema bump would make a report-wide literal gate block edits to properties Desktop itself wrote, and an AutomationId change would time out every model op without a clear cause.

*Raised by:* testing_scope

### P29

Test fixtures for sections 1, 2 and 5. Commit two fixtures.
(a) A structure twin generated from the Agents PBIX: same member order and compression, the UTF-16LE parts, a STORED random DataModel of the same size, and scrubbed names. The real file cannot be committed because it shows agent names.
(b) A Desktop-authored synthetic PBIX (DATATABLE data) with 3 pages, bookmarks and a navigator, a drillthrough page and a tooltip page, a group, a mobile layout, a custom theme, an image, CF rules, a TopN filter and visualInteractions.
Per-op goldens: make the same change in Desktop on (b), save, and commit the parsed-JSON diff; unit tests compare Studio's output semantically. The Desktop battery opens each Studio revision, screenshots it, UIA-saves it and asserts that the edit survived. A pixel probe maps the visual's rectangle into the chrome-bearing bridge PNG, checks the new color within a ΔE tolerance, and hashes the other visuals' regions to confirm they did not change. A preservation test asserts that parts outside an op's write set stay byte-identical.

*Why:* The unit-test PBIX has no $schema, report.json, themes, [Content_Types].xml, SecurityBindings or UTF-16 parts. The real file has one page and no bookmarks, groups or drillthrough pages. The page and reference ops therefore have no realistic coverage, and encodings such as font stacks and date category literals have no golden source. Studio's canvas can look right while Desktop keeps the old color.

*Raised by:* testing_scope

### P30

Section 8, the deterministic tier and the 4B tier.
Deterministic tier: parse by slots (target phrase + property word + value), which also covers declarative and question forms ('the title should be blue', 'could the bars be green'). Split clauses on commas, 'and' and 'then' when each clause carries its own property. Apply the resolved clauses as one revision with a per-clause done/asked receipt.
4B tier: restrict it to {an intent from the closed list, target indices from a page-scoped index, at most 2 slot values}, following the ReportVisualIntent pattern, and add Unsupported{capability} and Clarify{options} members. The model never emits SetVisualProperty, SetTheme, AddVisual or model ops. It gets one attempt with a 60 s cap, then chips.
Vague style requests go to a deterministic analyzer that offers suggestion chips, and nothing is applied without a choice.
Pending clarifications are stored as pending_edit and offered through an SSE edit_choices event as buttons.
Ship gate: evals/imported_edit has at least 60 cases on this dashboard's captions. At least 90% are handled with no model call (the _Model stub fails if called), there are 0 wrong targets, 100% of out-of-scope requests return Unsupported, and CPU p95 latency is recorded. The index stays within 3,000 bytes on the multi-page fixture.

*Why:* Without an Unsupported member, constrained decoding must emit some op: 'add a relationship between DimChannel and the sales table' becomes a SetFields and is reported as updated. columnChart alone has 307 properties, so the 4B model writes legend.location instead of position and fails after a repair loop of about 2 minutes. chat_context._sentences splits only on .!?: punctuation, so in 'make the bars green, add data labels and move the table left' only one intent applies.

*Raised by:* ux_model, testing_scope

### P31

Section 8 target resolution and section 9 canvas selection. Build the resolver index from:
- visible caption text (priority 15), fields and type;
- position words relative to same-type siblings (row and column bands by y and x), falling back to the page;
- the hue family of each visual's effective colors;
- plurals and quantifiers, which produce multi-target ops.
A visual title that is the only object named counts as the target: fix _without_names running before _REPORT_OBJECT. Canvas selection sets reportSelection and replaces the attached chip. Hit-testing honours z, with Alt-click cycling through stacked visuals. Every receipt names and highlights the resolved target, and 'this' with no selection asks which visual.

*Why:* App.tsx uses attachedContext ?? reportSelection, so a stale Ask AI chip sends 'make this font bigger' to the previously attached chart. 8a8ba163 (z 14000) sits inside 953a59a3, and without z-aware hit-testing a click can select the wrong one. 'Make Total MTD Sales green' is masked down to no object word and is never treated as an edit. 'The blue chart' matches all four charts.

*Raised by:* ux_model, renderer_preview

### P32

Section 8 ('change the target to 120K is an edit') and section 5 UpdateMeasure. A chat measure edit always renders the local what-if first, with an explicit 'Apply to report (needs Power BI, about 1 min)' button, and never applies automatically. After an apply, Studio searches visible captions and measure names for the old literal token and offers to update them through SetCaption and RenameMeasure.

*Why:* Verified: card_answers._WHAT_IF_CUE matches change, set and to, so 'change the 100K threshold to 120K' fires both the what-if and the edit path. A misread costs a full Desktop cycle. After the apply, the card caption 'Number of Agents Sold above 100K' and at least 8 measure names still say 100K while the numbers use 120K.

*Raised by:* ux_model

### P33

Section 7 batch and section 9 Preview. Model ops queue in a 'Pending Power BI changes (n)' tray and are applied in one Desktop cycle on an explicit click. Report-only edits never auto-reopen Desktop; the preview is marked 'out of date · Refresh' instead. A staged progress overlay locks editing during a cycle (Saving your Desktop work → Closing → Writing → Opening Power BI → Applying → Saving) and warns 'Power BI Desktop will open briefly; don't use it until Studio closes it'. Budget and measure the cycle with the 4B model loaded.

*Why:* One model edit with Desktop open costs a close, an open of about 20 s, the TOM write, a probe, a 5.5 s save, a close and a reopen of about 20 s: roughly 50 s with windows appearing and disappearing. Verified: service.py:509 unloads the 4B model before launching Desktop, so the next chat request pays a cold load of 63-77 s instead of about 24 s warm. Auto-reopening after every color tweak adds about 25 s per iteration.

*Raised by:* ux_model

### P34

Section 5 AddVisual and section 10 'new visuals'. Apply an automatic title rule: on for charts, off for tables and cards, and '<Y> by <Category>' when there is no title literal, mirrored in the importer and the renderer. Either add minimal renderers for textbox, shape, image and pie/donut, or limit ADDABLE_TYPES to kinds the renderer draws and label the rest 'preview in Power BI only'. Place new and duplicated visuals in free space, with an advisory overlap check.

*Why:* pieChart, donutChart, treemap, gauge, kpi and scatterChart are addable but render as an error box in Studio. exp4 and the smoke run placed the new chart at (1250, 10-20), on top of the Channel and Store slicers, and no check caught it. Desktop shows exp4's chart with the title 'Total Sales by ChannelName', while Studio stores 'ChannelName' and hides it.

*Raised by:* renderer_preview, testing_scope

### P35

Section 5 page ops and the Validation gate 'positions inside the page'. SetPageSize scales x, y, width and height proportionally by default, or refuses when visuals would fall outside the new size. SetPageBackground always writes transparency 0D together with a color, warns when an opaque image covers the color, and gains an image option that registers a RegisteredResources Image item under Desktop's naming, with restore from any ancestor revision.

*Why:* 'Make the page 1280x720' on this 1920x1200 page leaves 10 of 15 visuals out of bounds, and validate_document checks only x < 0 and y < 0. The theme's default page background transparency is 100, so a color set on a page from AddPage is invisible in Desktop. Once a Desktop save garbage-collects the PNG, no op can re-add it.

*Raised by:* pbir_fidelity, renderer_preview

### P36

Validation gate for DAX ('checked by the local engine before it is applied'). The local engine becomes advisory: when it cannot evaluate an expression, the pre-check is skipped rather than the measure refused. The authoritative check runs after the TOM write: INFO.MEASURES() or TMSCHEMA_MEASURES State and ErrorMessage, plus EVALUATE. Run the dax_engine differential on every new or updated measure.

*Why:* The local engine refuses DAX it does not support by design (memory note studio-dax-engine), so as a gate it would block valid measures. A single EVALUATE checks one filter context, and TOM accepts measures in an error state, which Desktop would then save into the PBIX.

*Raised by:* testing_scope

### P37

Packaging, runtime seal and readiness, which the design does not cover. Add --add-data lines for pbir_catalog.json and the schema snapshot in scripts/build-desktop-sidecar.ps1, and load them through importlib.resources. Add a THIRD_PARTY_NOTICES entry for @microsoft/powerbi-core-visual-schema (MIT). Reseal with --note at the end of each slice, and pin new modules with --add. Add Readiness.can_edit_report_layer and Readiness.can_edit_model (Desktop, bridge, pbi, and a passing contract receipt for the installed Desktop version), each with a UI state. State in section 7 that UIA runs as -EncodedCommand with PSExecutionPolicyPreference stripped, as desktop_uia.py already does, and add a unit test that asserts that environment.

*Why:* Verified: the build script bundles report_visuals.json and the layout catalog but not pbir_catalog.json, so the packaged onefile engine raises FileNotFoundError the first time the Format pane opens. THIRD_PARTY_NOTICES.md covers only pbixray. An unsealed module edit fails model calls mid-battery with a runtime hash mismatch.

*Raised by:* testing_scope

### P38

Scope and refusals: replace the short list with a capability matrix for 'full developer' editing. Each capability maps to an op family and a slice, or to an explicit refusal with the reason and the Desktop steps (reusing chat_context._desktop_steps).
Planned slices:
- Conditional-formatting authoring: field value, rules, gradients (unwrapped stop literals, per PBIR_FILLRULE_STOP_DOUBLE_WRAP), data bars and icons.
- Textbox rich text, image, shape, and buttons with actions.
- Bookmarks and the selection pane.
- Edit interactions.
- Drillthrough and tooltip pages.
- Sync slicers and slicer default state.
- Analytics lines.
- Visual-level number formats and display units.
- Group, align, distribute, format painter and bulk formatting.
- Per-column table formatting and field display names.
- Alt text and tab order.
- Mobile layout.
- Model edits beyond measures: calculated columns and tables, relationships, column properties, field parameters, calculation groups, RLS.
Explicit refusals:
- Power Query edits and refresh for this file (its F:\ sources are missing).
- Importing custom visuals.
- Publishing.
Other scope rules:
- Thin reports: report edits are refused or marked unverified until a thin fixture and a verification path exist; reportExtensions.json measures come in a later slice.
- Custom visual types fall back to container objects for title, background and border.

*Why:* All five lenses list the same missing developer capabilities, and the user asked for everything a user can do. Without a matrix, out-of-scope requests are either mis-mapped by the model or refused inconsistently. Verified: pbir_expr.property_spec returns None for an unknown visual type before it checks container objects, so even a custom visual's title edit is refused.

*Raised by:* pbir_fidelity, safety, ux_model, renderer_preview, testing_scope

### P39

Section 9 Format pane ('from the catalog, curated sections first'). Version 1 curates about 35 controls per visual family: Caption, Colors (a per-category and per-series list built from the rendered data), Data labels, Legend, Axes, Background & border, Size & position and Number format. Unset properties show 'Auto (theme)' filled from the theme's visualStyles and textClasses. A searchable 'All properties' list covers the rest. The inspector is widened to about 320 px or becomes a popover.

*Why:* The inspector column is 270 px (imported-report.css:23) and shrinks to a 250 px strip below 1100 px wide. The catalog has no default values, so opening Data labels on b404a461, where only fontSize is set, shows Show, Color and Position blank. It also offers no TopN/BottomN/Others list for coloring the categories separately.

*Raised by:* ux_model

## Findings rejected or already handled

- **testing_scope blocker: UIA save is run from .ps1 script files, which the machine's Restricted execution policy refuses outside the agent shell.** Already handled in code. src/ai_powerbi_analyst/studio/desktop_uia.py runs a fixed script as `powershell -NoProfile -NonInteractive -EncodedCommand`, which the policy does not cover, and strips PSExecutionPolicyPreference from the environment. scratchpad editexp/contract.py called it with the variable removed, and contract-log.json records save_invoke succeeding, saved_changed_file true and dirty_after_save false. What remains (state the mechanism in the design and add an env-asserting unit test) is part of priority 37.

- **ux_model blocker: SetColors(all) writes only the selector-less dataPoint.fill, so the per-category TopN/BottomN/Others overrides win and 'make the bars green' changes nothing while reporting success.** Outdated against the current code. _set with no selector calls pbir_expr.set_everywhere, which writes the value into the plain and id:'default' entries and into every narrower entry that already sets fill, including the scopeId TopN/BottomN/Others entries. So the bars do change. The real remaining defect is the opposite: overrides and measure-driven conditional formatting are overwritten silently. That is kept as priority 11.

- **pbir_fidelity: SetFont(values) appends a new selector-less entry on every cardVisual, so 94793535 and fb4c5aa3 keep 34pt in Desktop while Studio shows 28pt.** Outdated against the current code. set_everywhere updates the existing plain and {id:'default'} entries and every metadata entry that sets fontSize. It adds an entry only when none exists, as the smoke-run note in its docstring records. The remaining part of the finding, that Studio's renderer merges entries in list order, is kept in priority 11.

- **pbir_fidelity/ux_model: SetTitle always writes visualContainerObjects.title, so on cards and slicers it adds a second caption.** Partly outdated. report_edits._CAPTIONS already routes cardVisual to objects.label and slicer to objects.header, and visible_caption exists. Only the remaining gaps are kept in priority 15: the import index still uses the container title, legacy card and table column displayName are not handled, overlays are not detected, the renderer ignores the container title on cards, and quote un-doubling is wrong.

- **pbir_fidelity: RenameMeasure must leave queryRef unchanged because Desktop keeps it stable; rewriting queryRef is a defect.** Not a defect. _rename_references rewrites queryRef together with every metadata selector equal to the old queryRef, which is internally consistent, and queryRef is an opaque key. Keeping queryRef stable is kept only as the lower-risk choice inside priority 22, because it touches fewer places. The real defects are the missed From-alias Where references and the walk limited to visuals.

- **pbir_fidelity: encode_property writes labelDisplayUnits as a quoted string, so SetVisualProperty and data-label display units break.** Partly wrong. labels.labelDisplayUnits, the property SetDataLabels writes, is an enum in pbir_catalog.json and is encoded as a D number, which is correct. Only categoryAxis.labelDisplayUnits and valueAxis.labelDisplayUnits, which have type 'formatting', would be quoted, and only through SetVisualProperty. Kept in this narrower form in priority 13.

- **testing_scope: the evidence table's UIA-save and persistence claims have no receipt; the only log says D3_saved=false and measure_persisted=false.** Partly outdated. contract-log.json (13:22) records the UIA Save changing the file in 5.5 s and clearing the dirty flag. What is still unrecorded is that a TOM measure persists after a save, and the evidence sits in a temp scratchpad. The requirement to commit a contract script with receipts is kept as priority 28.

- **safety/testing_scope: hasUnsavedChanges is already true right at open, before any TOM write.** Corrected, not rejected. contract-log.json shows dirty_at_connect false and dirty_after_10s true with no action taken, so Desktop dirties itself shortly after opening a Studio-written file. The conclusion stands: the flag cannot signal user work. Kept in priority 4, with a contract step that compares the untouched original with a Studio-written file.

## Delivery slices

### S0 Desktop contract, process identity and fixtures

Commit evals/imported_edit/desktop_contract.py, grown from scratch contract.py, writing JSON receipts under evidence/. It records:
- Desktop ProductVersion, the bridge manifest hash and the schema versions;
- the dirty flag at open and after 10 s, for the untouched original and for a Studio-written copy;
- the Save-changes dialog's AutomationIds and a tested 'Don't save' path;
- UIA save;
- TOM create/update → save → pbix_extract (FormatString, 68 row counts);
- whether an external TOM rename with the report open makes Desktop rewrite PBIR references;
- exp5, the ThemeDataColor 0..11 × Percent probe with pixel sampling;
- Desktop goldens for M/datetime data literals and 'selection:selected';
- timings.
Implement Desktop identity by pid snapshot + path + create_time, and the modal-window check. Build the structure-twin fixture and the synthetic Desktop-authored multi-page fixture. Update the design's evidence table to cite the receipts and record the decisions: the ColorId table, the dirty-flag rule, the Don't-save id and the rename behaviour.

**Done when:** A receipt for Desktop 2.157.1354.0 exists in evidence/ and is referenced from the design doc. pytest loads both fixtures. A test starts two Desktop instances (one on an unrelated file), and Studio's identity function returns only the instance on the working copy. The receipt shows a dirty Desktop closed through the verified Don't-save id with no kill. The decisions table in the design is filled in from the receipt.

### S1 Lossless package, document and semantic diff

pbix_package and pbir_document with no ops. semantic_diff (priority 7), including the whitelist of Desktop normalizations. Preservation tests. The packaging work in priority 37: --add-data, notices, seal and the importlib.resources loading.

**Done when:** For the Agents file and both fixtures: a round-trip through Studio's writer gives an empty semantic_diff, every untouched part is byte-identical, and member order and compress types are kept. A Desktop open plus UIA save of a Studio-round-tripped file shows only whitelisted differences in semantic_diff. The built engine exe loads pbir_catalog.json in a smoke test. Reseal is done and the full pytest suite passes.

### S2 Revision store, working-copy protocol and history (two canary ops)

Content-addressed revisions, head, placed_revision and placed_hash. edit.lock, expected_head (409), pending idempotency keys and a server-owned per-report batch worker. The edit_batches journal with restart recovery, and Desktop launched outside the sidecar job. Per-revision derived metadata and cache keys (priority 19), with a lint test that only report_revisions reads archived_path. The canvas head prop and stale-response dropping (priority 6). Previews keyed by head hash. The batch order: ask → save → close → wait for exit → adopt → write. Conflict revisions. History with newest-only Undo and Restore. Delivery checks (priority 10). Only SetCaption and SetPosition are exposed, through the API with no chat.

**Done when:** Automated tests show:
- edit → render → undo → render asserts the spec values each time;
- an edit with Desktop closed followed by 'Open in Power BI' opens head bytes;
- a user save made while head != placed becomes a conflict revision whose parent is placed_revision;
- two concurrent edits on the same head produce one 409;
- killing the sidecar at each journal state leaves head consistent after restart, and the Desktop pid is still alive;
- Replace original is refused when the source hash has changed.
In a manual Desktop run, a canary edit made while the user has unsaved Desktop changes produces the consent prompt and never a silent save.

### S3 Formatting core with protected conditional formatting

pbir_expr: open selector records, the effective-value precedence resolver, literal typing (catalog subtypes, suffix reuse, model data types, the font table) and the verified ThemeDataColor table from S0. set_everywhere confirms before replacing overrides or non-Literal values. The color inventory. The ops SetColors, SetFont, SetContainer, SetDataLabels, SetLegend, SetAxis, SetVisualProperty, Arrange and SetHidden. Renderer work: reads every token those ops write, specificity merge, z/hidden/groups, fidelity and visible_in flags, and the formatting-only render path. The classified offline validator gate (priority 27). The curated Format pane (priority 39).

**Done when:** On fixture (b), each op's output matches its Desktop golden semantically, and every op survives a Desktop UIA save with an empty survival diff. On the Agents twin: a pixel probe on the bridge screenshot confirms 'all bars green' and 'TopN only orange' on da32cdbb/b404a461; fb4c5aa3's value font change shows in both Studio and Desktop; and e82e02f9's four Measure fontColor entries are unchanged unless the confirmation was accepted. Studio's compiled spec carries each new value. Duplicating slicer 54be5cf0 passes the gate with an advisory note.

### S4 Theme and color replacement

SetTheme with the override inventory ('clear N overrides' or 'rewrite N overrides'), SharedResources customTheme copy-and-repoint, and base+custom deep merge in pbir_document and the renderer. ReplaceColor across visual literals and ThemeDataColor, with DAX-embedded colors turned into pending measure edits and image-baked colors refused. Opaque background-image detection with Remove/Replace/Keep.

**Done when:** A theme dataColors change is visible on a visual with no explicit colors, both in Studio and in a Desktop screenshot. On the Agents twin, 'use Arial' reports the number of pinned overrides, and the 'clear overrides' choice changes titles visibly in Desktop. ReplaceColor #1E90FF→#1F3864 recolors all 4 charts, lists the 4 SVG measures as pending, and survives a Desktop save.

### S5 Structural visual, page and filter operations

The report-wide reference index (visual, page and filter names). Delete and Duplicate fix-ups: filter names, tabOrder, interactions, bookmarks, recursive groups. The per-type role table for AddVisual. ChangeVisualType carry-over. SetFields and SetSort by field identity with SummarizeBy and HierarchyLevel. Page ops with reference remap, SetPageSize scaling and SetPageBackground transparency and image. Visual, page and report filters with Desktop encodings, and renderer per-visual filter tolerance. AddVisual placement and auto titles, limited to renderer-drawable types or flagged Power-BI-only.

**Done when:** A golden test pins the reference-index counts on fixture (b). Each op's output on fixture (b) opens in Desktop, survives a UIA save, has no dangling references (checked by Studio's structural validator) and adds no blocking diagnostics. A role-table unit test passes for every ADDABLE type: a kpi puts Total Sales in Indicator, and a scatter chart can be added. Each filter variant matches its Desktop golden. A TopN page filter leaves the rest of Studio's canvas rendering.

### S6 Deterministic chat editing

Slot parser with clause splitting. The resolver index (visible captions, relative position words, hue families, plurals). Canvas selection wired to chat, and z-aware hit-testing. SSE events edit_applied and edit_choices, with pending_edit. Undo, redo, restore and 'what changed' intents. The what-if-first rule for measure edits. visible_in receipts. The deterministic suggestion analyzer for vague style requests.

**Done when:** evals/imported_edit/cases.jsonl holds at least 60 cases written against the Agents captions, with what-if/edit pairs, out-of-scope requests and questions that must not edit. At least 90% run with no model call (the _Model stub fails if called), 0 target the wrong visual, what-if pairs are routed 100% correctly, and every receipt names the resolved target.

### S7 4B fallback tier

Model output restricted to {intent, target indices, at most 2 slots}, with Unsupported and Clarify members and the refusal catalog built from the capability matrix. One attempt with a 60 s cap, then chips. Page-scoped index kept within 3,000 bytes.

**Done when:** On the eval cases routed to the model: 0 wrong targets, 100% of out-of-scope requests (relationships, bookmarks, RLS, Power Query) return Unsupported, exact-op accuracy and CPU p95 latency are recorded against a budget, and the prompt is at most 3,000 bytes on the multi-page fixture. The run uses the Qwen 4B model under the memory-noted CPU limits.

### S8 Model operations through Desktop

CreateMeasure and UpdateMeasure first, then RenameMeasure (measure reference index, including From-alias Where) and DeleteMeasure (refused on model dependents). The staged single-revision transaction with compensating TOM rollback, and TOM State/ErrorMessage plus EVALUATE checks, with the local engine advisory. The 'Pending Power BI changes' tray, the staged progress overlay and no auto-reopen. Readiness.can_edit_model, gated on a passing contract receipt for the installed Desktop version.

**Done when:** Fault-injection tests fail a fake desktop_session at each step and assert that head is unchanged and the batch recovers after restart. A Desktop battery shows create, update, rename and delete persisting after UIA save and pbix_extract. After renaming 'Rank Filtering Dynamics', no reference to the old name remains, including e82e02f9's Where clause, and the agent table still filters to rank 1 in a Desktop screenshot. DeleteMeasure 'Rank Filtering Employyees MTD' is refused and names 'Rank Filtering Dynamics' and the agent table.

### S9+ Remaining developer capabilities, one per slice

In this order, following the capability matrix: conditional-formatting authoring (field value, rules, gradient, data bars, icons); textbox rich text, image upload, shapes and buttons with actions; bookmarks and the selection pane; edit interactions; drillthrough and tooltip pages; sync slicers and slicer default state; analytics lines and visual-level number formats; group, align, distribute, format painter and bulk formatting; per-column table formatting, field display names, alt text and tab order; mobile layout; model beyond measures (calculated columns and tables, relationships, column properties, field parameters, calculation groups, RLS); thin-report support with reportExtensions measures.

**Done when:** Each slice ends with Desktop goldens on fixture (b), a passing survives-save check, renderer fidelity flags set for its properties, new eval cases in the chat battery, a reseal and a green full pytest run. The capability matrix moves that capability from 'planned' to 'shipped'.
