# Existing Power BI reports

Choose **Open Power BI report** from the workspace menu or command search, or
select a `.pbix` in **Choose local data**. Opening creates a separate workspace
and hashes/preserves the source before reading it.

The importer reads modern PBIR report definitions and legacy `Report/Layout`,
including original page order, visual types, positions and field bindings. All
original bytes remain in the archived PBIX, including formatting, resources and
features Studio does not render. The Report tab opens directly in **Layout & fields**:
local SVG charts, cards, sortable HTML tables and slicers, backed by the working
copy's native Power BI calculation engine. Slicers and chart selections update
the visual queries together; Reset filters restores saved slicer selections.
Studio selections never write to the report or change Desktop slicers.

The first renderer supports PBIR column/bar/line/area charts, cards, single-column
slicers and tables using explicit measures and column projections. Report/page
filters, visual filters, saved slicer choices and original sort order are compiled
through a closed field/filter grammar. Unsupported filters fail visibly rather
than silently showing unfiltered data. Matrix hierarchies, field parameters,
implicit aggregations, bookmarks and custom visuals need separate support.
Studio reads static model format strings and formats display cells through the
native engine while retaining numeric values for sorting, filtering and chart geometry.
It also applies saved card labels and font sizes, reference values/comparisons,
category colors, table font-color measures and positive/negative data bars.
Card SVG measures are evaluated in the same filter context and displayed only after
an inert SVG allowlist rejects scripts, external references and unsupported markup.
Embedded PNG/JPEG page backgrounds are reused behind the interactive components;
these assets are not screenshots of the report. Slicer captions and button tiles
are preserved. Full Power BI style parity is not claimed: dynamic format-string
expressions, all theme/style presets, numeric slider behavior, complex data-bar
bounds and advanced custom visual settings still need dedicated support.

Formatting metadata is cached by the verified archive hash. Static measure formats
are read in an isolated, disk-backed metadata subprocess (90-second deadline),
without decoding fact rows or exporting connection information. Auxiliary formatting
measures count toward the existing 12-field limit. Local page images are limited to
4 MiB and SVG measure text to 100 KB; no remote asset URLs are fetched.

The single report view populates the original visual positions with live Studio
components and retains the field inspector. Readable, Fit width and fixed zoom
controls are inside the canvas Report options menu alongside refresh and reset; selecting a slicer title exposes its full
search/filter controls. There is no alternate grid or screenshot view in the UI.

PBIXRay 0.15.5 extracts embedded VertiPaq tables, measure expressions and
relationships offline. Tables are normalized through the existing Parquet
snapshot pipeline and can be browsed, filtered and sorted in Data. Model exposes
all original measures and relationships. Select an original measure in DAX Lab
to replace the scratchpad and run it using the working copy's Desktop engine.
Field selection does not alter the formula; insertion is an explicit action.
**View by** evaluates the same calculation grouped by any model field without
rewriting the measure. Page slicer selections are not scratchpad filter context.
Managed local workspaces continue using the bounded snapshot evaluator.
Chat receives bounded report/model context and cannot overwrite the native report.

**Open in Power BI** opens a separate working copy in the installed Desktop
application. The original source and archived copy are never used as an editable
Desktop target. Native previews bind to the exact working-copy process and its
child calculation engine; an unrelated open report is never used. Cached images
remain available after Desktop closes. Live refresh and DAX require the working
copy to be open. Existing managed-project build/approval protections are unchanged.

Native operations run one at a time, with a 15-second DAX engine timeout,
100-row/40-column DAX scratchpad limit and a 12 MiB page-image limit. Interactive
pages batch at most 40 visuals with up to 12 projected fields and 100 displayed
rows each, plus one overflow row per visual. Truncated slicer lists provide
server-side search; truncated tables/charts show a limit notice. Failed
refreshes retain the previous image. Queries use temporary files and never a
command shell; temporary query/capture files are removed on success or failure.

Thin reports without embedded rows import their layout with a clear notice; the
importer never opens remote connections or executes Power Query. PBIP remains a
separate compatibility-inspection flow. Encrypted/unsupported models fail with
an actionable error; no replacement dashboard is silently generated.

Resource bounds: 512 MiB input, 32 MiB report JSON, 200 pages, 4,000 visuals,
200 decoded tables, 500 columns per table, 50 million rows per table, and a
three-minute decoder deadline. Decoding runs in an isolated process, uses a
disk-backed model and 50,000-row output chunks. Its memory budget adapts to free
RAM (55%, capped at 2 GiB), with memory/disk headroom checks and process cleanup.
Dictionary-heavy models can still exceed these bounds and must use Desktop.
