# Native Power BI authoring: architecture and delivery plan

Decision date: 2026-09-20. This is a capability audit and implementation plan,
not a claim of full Power BI parity.

## Product direction

### Delivered: date filters on individual visuals

Report's **Date filter** command (also available in visual properties and through
chat) selects a reviewed date field from the measure's source table. **Apply &
preview** saves one visual's inclusive date range and revalidates its bounded
aggregate preview. A through-date compiles as an exclusive next-day bound, so
timestamps throughout the chosen day remain included. Clearing a filter restores
the original aggregate. The measure expression and other visuals are unchanged.

Chat resolves selected page/visual IDs before entering new-visual generation;
without a selected or uniquely named visual, it opens a chart picker. Date-filter
commands do not require an LLM call. The filter survives draft hashing, undo and
Power BI compilation as `visual.json.filterConfig`. Date-field validation rejects
invented columns, non-date fields and incompatible source tables. The current UI
supports direct governed aggregates and ratio/margin/variance measures; details,
advanced DAX, related-dimension date fields, relative periods, page-wide filters
and interactive slicer visuals need additional implementation.

Verification evidence: `evidence/report-date-filter-20260920-215429/`. Fresh
timestamp fixtures check full-day boundaries and unchanged sibling totals. The
emitted filter passes Microsoft's `filterConfiguration/1.3.0` JSON schema. Native
Power BI Desktop rendering and interactive filter behavior have not been tested.

Reference: [Microsoft's visual-level filter workflow](https://learn.microsoft.com/en-us/power-bi/create-reports/power-bi-report-add-filter).

Studio should become a local AI authoring layer that produces native Power BI
reports. Qwen discusses intent and proposes typed edits. Deterministic code owns
field binding, query planning, validation, file compilation and receipts. Power
BI Desktop remains the authority for final DAX evaluation, visual rendering and
report interactions. Preserve the existing local-first mode, resource guards,
explicit build approval and reversible draft workflow.

If exact native interaction inside Studio is required, add an optional Power BI
Embedded adapter. It introduces Microsoft authentication, service deployment and
licensing/capacity decisions; it is not a local offline replacement for Desktop.
Microsoft exposes APIs to create visuals, bind fields and change their properties
after an embedded report has rendered. A simple secure iframe is view-only, so it
does not meet authoring requirements. See [Embedded authoring](https://learn.microsoft.com/en-us/javascript/api/overview/powerbi/create-add-visual)
and [embedding options](https://learn.microsoft.com/en-us/power-bi/developer/embedded/embedded-analytics-power-bi).

## What the audit established

The installed `powerbi-report-author` catalog returned 57 identifiers, including
aliases, deprecated visuals, navigation objects and visuals with external
dependencies. This is not 57 tested Studio charts. Preserve its discovery receipt
under `evidence/report-capabilities-20260921-010319/installed-visual-catalog.json`.

Studio's current closed draft supports 13 types across four recipe families.
Analytical visuals have one measure and one grouping field. Tables have up to
eight detail columns. Matrix, multi-measure tables, separate series, combo axes,
hierarchies, slicers and general formatting are absent from the active Studio
contract. Pie, donut, funnel and waterfall currently show aggregate data locally;
the final shape is deferred to Power BI. A larger LLM cannot fix missing actions.

There is existing reusable infrastructure: `VisualTypeCapability`,
`VisualRoleBinding`, `VisualPlan`, the installed catalog reader, PBIR compilation,
source profiling, bounded DuckDB queries and build transactions. Extend and
connect these pieces; avoid a second independent report engine or LLM-generated
PBIR fragments.

## Foundation implemented in this pass

`studio/report_visuals.json` is now the shared authoring catalog for the Report
picker, property type lists, minimum grid sizes, Python recipe validation, Qwen
revision context and authenticated `GET /v1/report-capabilities`. Discussion
prompts also receive a compact capability summary for report questions.

The catalog explicitly distinguishes currently accepted authoring types from
missing features and local preview limitations. Native discovery does not
automatically enable anything. The Add visual menu now exposes all 13 currently
accepted types directly. No new visual is declared production-qualified by this
change; the existing data, inference, query and approval limits are unchanged.

There is now a small typed model path for adding one visual to a named existing
page (`report_intent.py`). Qwen selects the type and zero-based field/measure
indices; Python resolves them to reviewed IDs and owns placement, validation and
preview. The context selects up to three relevant tables, eight fields per table
and six measures, with a 2,750-byte input budget. It never includes source rows or
the full existing report. Output is capped at 256 tokens, at most one semantic
correction is allowed, and the 4,096 conservative runtime context guard remains
unchanged. Unknown indices and invalid field roles fail validation. Missing or
unsupported selections leave the draft unchanged. The larger general revision
path remains a migration target; its output cap was reduced from 1,200 to 768 to
respect the runtime's existing 1,024-token maximum.

A fresh isolated task passed with Qwen2.5-Coder 1.5B and retrieved examples: add a
donut chart of Sales by Region. It needed one correction and completed in 15.75
seconds on this host. Exact aggregate values were Aster=451, Brook=268, Cedar=287;
the native plan preserved `donutChart` and its bindings. This is one successful
task, not a benchmark or full visual qualification. Native Desktop rendering was
not tested. Requests, both model outputs, query results, model/resource evidence
and the native plan are preserved in
`evidence/report-visual-selection-20260921-012608-c31dd8/`. Earlier preparation
failures remain in separate evidence directories. Previous runtime seals were
preserved in `evidence/report-capabilities-seal-20260920-212553/` before using the
existing developer reseal tool; model weights and resource guards did not change.

Report discussion also reserves room for capability context. Only whole optional
references are omitted when necessary; the retrieval receipt and citation footer
reflect the references actually supplied. User requests and schema are not cut.

## Shared document and capability model

Replace the restrictive binding fields incrementally with the existing typed
`VisualRoleBinding` representation. A versioned visual document should hold:

- Native type ID and stable visual/page IDs.
- Ordered field wells: Category, Series, X, Y, Rows, Columns, Values, Size,
  tooltips or other roles actually described by that visual's catalog.
- Approved field/measure IDs and their source tables; no arbitrary SQL or paths.
- Separate visual/page/report filters, sorting, formatting, layout and actions.
- Capability/contract version plus evidence that qualifies each supported path.

Keep adapters for existing drafts. Do not rewrite historical receipts or silently
reinterpret old bindings. Unknown options fail validation with actionable errors.
Validate role cardinality and kinds, references, relationship paths, field types,
formatting properties and interactions. Do not treat catalogued as equivalent to
previewed, compiled, refreshed or visually verified.

The UI and Qwen must read the same registry. Generate field wells and property
controls from it. Register a preview adapter, native compiler adapter and fixtures
for each visual. Record separate qualification states for schema, semantic
binding, local preview and Desktop refresh/render, against the tested versions.
PBIR supplies public JSON schemas and separate files for pages, visuals and
bookmarks; it is the appropriate native document boundary. See
[Microsoft's PBIR documentation](https://learn.microsoft.com/en-us/power-bi/developer/projects/projects-report).

## Qwen interaction and tools

Use a short bounded loop:

1. Resolve the user's selected page/visual and relevant schema/measure context.
2. Retrieve matching worked examples and capability definitions only.
3. Discuss ambiguity when it changes the calculation; preserve a draft plan.
4. Propose typed operations such as `add_visual`, `bind_fields`, `set_filter`,
   `set_sort`, `set_format`, `set_interaction` and `move_visual`.
5. Validate the complete atomic edit against the current draft hash; execute a
   bounded preview and return structured feedback.
6. Allow the existing single correction attempt. Apply valid edits to the local
   draft with undo; retain explicit review before any Power BI write.

Keep read operations (`list_visual_types`, `describe_visual`, `search_fields`,
`inspect_measure`, `preview_visual`) separate from draft mutations and build.
Expose these as closed server operations, not shell/file access. The first pass
adds capability context and the bounded `add_visual` selection path; the broader
operation set above remains to implement.

Example intent: "Create a matrix of Sales and Profit by Region, with Segment
across columns." The resulting document binds Region to Rows, Segment to Columns,
and the reviewed Sales/Profit measures to Values. Follow-ups such as "only West",
"add margin", or "show months instead" modify filters, values or field wells;
they should not rewrite the measure or require transferring code from chat.

## Delivery sequence and completion gates

| Stage | Features | Required proof before enabling |
| --- | --- | --- |
| 1: Field wells | Matrix; multi-measure table; ordered row/column hierarchies; value formatting; totals | Typed manual and Qwen construction; exact grouped values and totals on fresh fixtures; schema-valid PBIR; Desktop refresh/render |
| 2: Chart families | Multi-series bars/columns, 100% stacks, line/area, combo, scatter/bubble, pie/donut, treemap, waterfall, funnel, ribbon, card/KPI/gauge | Correct axis/series roles, sorting and missing-value behavior; matching native outputs; representative formatting |
| 3: Report behavior | Slicers, filter panes, cross-filter/highlight, drill-down/through, tooltips, bookmarks, navigation, accessibility | Interaction tests across multiple pages; undo/redo and round-trip preservation |
| 4: Extended ecosystem | Maps, approved custom visuals, external/AI visuals, optional Embedded integration | Dependency and authentication handling; versioned native qualification; explicit unsupported/offline states |

Start with matrix because it forces the missing multi-role/multi-measure design
to be solved correctly. Its native roles are Rows, Columns and Values; a detail
table is not a substitute. Matrix drill, subtotals, expand/collapse and formatting
are separate capabilities, not implied by drawing a pivoted HTML table. See
[Microsoft matrix guidance](https://learn.microsoft.com/en-us/power-bi/visuals/power-bi-visualization-matrix-visual).

Stage 1 fixtures must cover sum, average, distinct count, ratios, blanks, repeated
labels, dates, multiple measures, row/column totals, high cardinality and approved
cross-table paths. Totals must reevaluate the measure in total filter context;
never sum displayed averages, ratios or a truncated sample. Run multiple measures
over the same grouped result domain rather than joining independent top-N queries.

## Performance and evidence

Keep Qwen off the rendering/query hot path. Send metadata and small result
summaries; never stream full imported tables into its prompt. Compute aggregates
against Parquet/DuckDB or an approved remote query adapter. Virtualize long tables
and matrices. Cache by snapshot, semantic model, measure, filters, field wells and
query contract; invalidate on any semantic change. Distinguish preview sampling
from aggregation over full data. Keep all current RAM, concurrency, query,
timeout, row and repair guards until a separately measured budget change is made.

For each stage, preserve new unique request, answer, validation, query and build
receipts. Measure fresh tasks for intent fidelity, correct artifacts, native
render success, retries, elapsed time and peak memory. Verify on 8 GB/16 GB
hardware or explicitly label simulated pressure. Native Desktop verification and
cloud integration cannot be inferred from unit tests or catalog presence.

The next functional milestone is Stage 1. Full Power BI authoring remains a
multi-stage product effort; this pass supplies its audited foundation and plan.
