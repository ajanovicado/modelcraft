# Model health (best practice analyzer)

`src/ai_powerbi_analyst/studio/model_health.py` runs the checks an experienced Power BI developer
runs over a semantic model. Tabular Editor's Best Practice Analyzer is the reference for *what* to
check; the rules, thresholds and wording are Studio's own. It is pure and deterministic. No finding
comes from the language model: the local model may phrase or pick findings, but never produce one.

## API

```python
usage = report_usage(document)                        # PbirDocument -> Usage
stats = stats_from_parquet(files, wanted=stats_wanted(facts))   # optional
health = check_model(facts, usage=usage, stats=stats)  # -> Health
health.as_dict()        # {known, findings, summary, checked, notes}
describe(health)        # plain chat text
is_health_request(msg)  # chat routing
findings_for(health, [ObjectRef("column", "Sales", "Amount")])  # badges, receipts
```

- `facts`: the **full** facts from `pbix_extract.read_model_facts` (`head_model_facts`). The
  stored copy (`ImportedReport.model`, `model_facts.stored`) drops measure DAX. With it, or with
  any formula cut short, every "used nowhere" rule would be wrong, so `check_model` skips them and
  adds a note.
- `usage`: `report_usage(document)` scans visuals, filters at every level (including `From` aliases),
  sorts, conditional formatting, bookmarks, user hierarchy levels, and the automatic date hierarchy's
  `PropertyVariationSource`. It also reads report measures (`reportExtensions.json`) and saved DAX
  queries (`DAXQueries/*.dax`). Passing `None` or `Usage.unknown()` means "no report read": the usage
  rules are skipped, with a note.
- `stats`: `{(table, column) folded: ColumnStats(rows, distinct, blanks)}`, from the snapshot's rows
  (`stats_from_frames` / `stats_from_parquet`). Only the relationship-key and high-cardinality rules
  use it.

`Finding = {id, rule, category, severity, objects, message, why, fix, fix_summary,
fix_needs_desktop, fix_confirm, fix_outside_supported}`:

- `message` and `why` are one plain sentence each. They name objects only: never a formula, Power
  Query text, an error text (it can quote the formula) or a data value. Tests check this.
- `fix` is a list of model-family operations (`report_edit_model`) or measure operations
  (`update_measure`, `delete_measure`, `rename_measure`). Each fix parses as one batch
  (at most 50 steps; a longer one is split into `<id>:1`, `<id>:2`). Every fix needs Power BI
  Desktop and the user's confirmation, and nothing is ever applied automatically.
- `fix` is None when the right fix is a judgement call, or is a DAX rewrite (the planner drafts
  those later). In that case `fix_summary` says what to do by hand.
- `fix_outside_supported` is true when the fix uses a change that Microsoft does not list as
  supported for external tools (`report_edit_model.MICROSOFT_SUPPORT == "outside"`, for example
  `mark_date_table`, `rename_column` or `rename_table`). The `fix_summary` then says so: Studio
  makes such a change only where a model probe passed for the running Desktop version.

`summary = {problems, suggestions, by_category, by_rule}`. The order is fixed: problems first, then
by category (correctness, performance, usability, formatting, naming), then by rule and id.

**Granularity:** each relationship rule gives one finding per relationship. Each column or measure
rule gives one finding per table, listing every object. A model-wide rule gives one finding. A
table's objects that have no automatic fix get their own finding (`<table>:by-hand`).

**Severity:** a *problem* can give wrong numbers or break a refresh. A *suggestion* costs size or
speed, or is tidiness.

## Rules

| id | category | severity | when | fix |
|---|---|---|---|---|
| `broken_object` | correctness | problem | measure/column state is an error, or has an error text | none |
| `date_table_not_marked` | correctness | problem when the table joins on a non-date column or is a fact (many side) table, else suggestion | TOTALYTD/DATESYTD/... (dates argument) on a table not marked as a date table | `mark_date_table` (outside list) when one column and not a fact table |
| `relationship_key_duplicates` | correctness | problem | stats: one side repeats values | none |
| `relationship_key_blanks` | correctness | suggestion | stats: one side has blanks | none |
| `two_way_relationship` | correctness | suggestion | crossFilter both, not 1:1 (1:1 always filters both ways) | `update_relationship cross_filter single` |
| `many_to_many_relationship` | correctness | suggestion | cardinality M:M | none |
| `floating_point_column` | correctness | suggestion | used Double column named as money (amount, price, cost, sales...) or quantity (quantity, qty, units); not rate/ratio/margin/percent | calculated column: `set_column_properties data_type fixed_decimal/whole_number`; data column: none (Power Query) |
| `dax_division` | correctness | suggestion | `/` whose divisor is not a non-zero constant (measures, calculated columns, calculation items) | none (planner) |
| `auto_date_time` | performance | suggestion | model autoDateTime on, or hidden LocalDateTable tables / variations | none: Desktop's File > Options steps; lists the visuals using an automatic date hierarchy (Studio refuses the auto date/time switch, see model spec review #2) |
| `unused_column` | performance | suggestion | visible column used by nothing | `set_column_properties hidden` (to also shrink the file, remove in Power Query) |
| `unused_hidden_column` | performance | suggestion | hidden data / calculated-table column used by nothing | none (Power Query, or the table's formula) |
| `unused_calculated_column` | performance | suggestion | hidden calculated column used by nothing | `delete_column` |
| `high_cardinality_column` | performance | suggestion | stats: text with >= 100,000 distinct values that are >= 50% of rows; date/time with time of day and > 36,525 distinct values | none |
| `calculated_column_as_measure` | performance | suggestion | calculated column only used inside SUM/AVERAGE/MIN/MAX/MEDIAN/PRODUCT, SUMX-family expression arguments or visual aggregations | none (planner) |
| `dax_iferror` | performance | suggestion | IFERROR / ISERROR | none (planner) |
| `dax_filter_table` | performance | suggestion | `CALCULATE(..., [KEEPFILTERS(]FILTER(Table, <condition on Table's own columns only>))` | none (planner) |
| `visible_key_column` | usability | suggestion | visible many-side column of a many-to-one relationship; or visible one-side whole-number `...Key`/`...ID` column the report does not show | `set_column_properties hidden` |
| `summarized_key_column` | usability | suggestion | visible number column summarized Sum/Default that is a relationship end, a sort-by column, or whose last word is key/id/code/year/month/quarter/week/day/number/no/order/sort/index/rank/zip/postcode/phone | `set_column_properties summarize_by none` |
| `unused_inactive_relationship` | usability | suggestion | inactive, and no formula (model or report) names it in USERELATIONSHIP/CROSSFILTER | `delete_relationship` |
| `unused_measure` | usability | suggestion | visible measure used by no visual, filter, formatting rule, bookmark or formula | none (judgement: other reports may use it) |
| `unused_hidden_measure` | usability | suggestion | hidden measure used by nothing | `delete_measure` |
| `measure_folders` | usability | suggestion | a table with more than 15 visible measures, some outside display folders | none |
| `missing_descriptions` | usability | suggestion | visible tables, columns, measures without description (one finding) | none |
| `measure_no_format` | formatting | suggestion | visible Double/Decimal measure without format string or dynamic format | none (the right format is a judgement) |
| `name_whitespace` | naming | suggestion | leading/trailing spaces, tabs or line breaks in a name | `rename_measure` / `rename_column` / `rename_table` to the cleaned name (none when that name is taken) |
| `name_case_clash` | naming | suggestion | tables, measures and columns whose names are equal ignoring case but spelled differently | none |

**What counts as "used":**

- **The report:** visuals (fields and aggregations), filters, sorts, formatting, bookmarks,
  automatic date hierarchies and user hierarchy levels.
- **DAX:** measures, dynamic format strings, calculated columns and tables, calculation items and
  selection expressions, role filters, report measures and saved DAX queries.
  - A bare `[Name]` that is not a measure counts for every column with that name.
  - A calculated table that names a table on its own (`Date = DateAutoTemplate`) uses all of that
    table's columns.
- **The model:** relationship ends, sort-by columns, hierarchy levels, a date table's date column,
  and a calculated table column's source column.

Uses are direct, not transitive: a measure that only an unused measure calls still counts as used,
so deleting a hidden unused measure never breaks another.

The unused rules skip:

- tables Desktop wires itself: auto date/time tables, calculation groups, field and what-if
  parameters;
- columns of marked date tables.

The type, summarization and cardinality rules skip columns that the unused rules already report.
Relationships to auto date/time tables are left to `auto_date_time`.

## Measured on the Agents Performance dashboard (Desktop 2.157, read-only copy)

The check took 109 ms, found 0 problems and made 15 suggestions. The results were the same with
column statistics from the decoded rows: every one-side key is unique with no blanks, and no
column has high cardinality.

| finding | objects | why it is true |
|---|---|---|
| `floating_point_column:FactSales` | DiscountAmount, TotalCost, SalesAmount | Double columns summed by measures. The unused UnitCost, UnitPrice and ReturnAmount are left to the unused rule. |
| `dax_division:# Measures` | Employees Avg Total Discount - Adjusted, MTD Discount per Transaction | Both divide one measure by another with `/`. The other 28 divisions use DIVIDE. |
| `unused_column:Actuals-vs-PctChange` | Order (1) | The slicer uses Top-Bottom-Field. Order is not its sort-by column, and no formula names it (the `[Order]` references are all qualified `'Clustered Employees'[Order]`). |
| `unused_column:DimEmployee` | 9 columns | A text search of every report part, DAX query and formula finds none of them. |
| `unused_column:DimStore` | 8 columns | Same check. |
| `unused_column:FactSales` | 6 columns | Same check. |
| `visible_key_column:FactSales` | DateKey, channelKey, StoreKey | These are the many-side keys of the three fact relationships, and all are visible. |
| `visible_key_column:DimStore` | StoreKey, EmployeeKey | StoreKey is the one-side whole-number key and the report does not show it. EmployeeKey is the many-side key to DimEmployee. |
| `visible_key_column:DimChannel` | ChannelKey | This is the one-side whole-number key and the report does not show it. DimEmployee[EmployeeKey] is left alone because the table visual shows it as "Agent ID". |
| `summarized_key_column:Clustered Employees` | Order | Whole number, Summarize by Sum. The measures read it with SELECTEDVALUE as a group order (1, 2, 3). |
| `unused_measure:# Measures` | 23 | No report part (by literal search) and no formula names any of them. They are mostly the generated % variants and the unwired "... CF" measures. |
| `measure_folders:# Measures` | 12 of 100 | Total Sales, Profit and others have no display folder. |
| `missing_descriptions` | 9 tables, 51 columns, 101 measures | No visible object has a description. |
| `measure_no_format:# Measures` | 42 | Visible Double measures with neither a format string nor a dynamic format. |
| `name_case_clash:channelkey` | FactSales[channelKey], DimChannel[ChannelKey] | The two ends of one relationship are spelled with different case. |

No rule fires on the Date and DateAutoTemplate tables, which are marked and generated from a
template. It also does not fire on the what-if parameter or on the 102 measures' time
intelligence (they use the marked `'Date'[Date]`). The real-file test
(`test_the_real_agents_dashboard`) pins these ids and object counts. It also checks that no
measure formula appears in the output.

## Unverified / open

- `summarized_key_column`'s fix uses `set_column_properties summarize_by`. The model family lists
  its `SummarizationSetBy` annotation as unverified.
- `mark_date_table` is outside Microsoft's list. Live probe run 2 on Desktop 2.157 kept it.
- `auto_date_time` finds hidden date tables through `autoDateTable` or `ShowAsVariationsOnly`.
  The Agents file has auto date/time off, so no Desktop-authored file with local date tables was
  checked here.
- `MICROSOFT_SUPPORT` and `OUTSIDE_SUPPORT` are imported from `report_edit_model`. That module's
  owner added them this round, so they must stay public.
- The rules are heuristics where a name decides: money and quantity words, key words, and
  surrogate keys. They were tuned to raise no false positive on the Agents file. Each word list is
  a named constant.

## Wiring spec (for the integration agent)

**Service** (`ImportedEditingService`):

- `model_health(report_id: str, *, stats: bool = False) -> dict[str, Any]`
  - Uses `facts = self.head_model_facts(report)` (the full facts, never `report.model`), and
    `document = PbirDocument(PbixPackage(head revision path))`.
  - Calls `usage = model_health.report_usage(document)`.
  - When `stats` is true and the payload's `model.studio.data.stale` is false, builds
    `stats = model_health.stats_from_parquet({t.table.name: t.normalized_parquet_path for t in
    snapshot.tables}, wanted=model_health.stats_wanted(facts))`.
  - Returns `model_health.check_model(facts, usage=usage, stats=stats).as_dict()`.
  - Cache by (head revision hash, facts cache key, snapshot id, stats).
  - When the facts are unknown, return the `known: false` result; do not raise.
- `model_health_fix(report_id: str, finding_id: str, *, base_hash: str, confirmed: bool) -> receipt`
  - Recomputes health and finds the finding by id. If it is gone, answers 409 "This was already
    fixed or changed".
  - Without `confirmed`, raises `NeedsConfirmation("model_health_fix", fix_summary)`.
  - Otherwise runs `finding.fix` through the normal edit pipeline as one batch (model ops go
    through Desktop; gate on `DesktopReadiness.can_edit_model_objects`, and ops outside
    Microsoft's list on the probe receipt, like any model op).
  - The `tests` workstream's `gate` runs before commit, like any model change.

**API routes:**

- `GET /api/imported/{report_id}/model-health?stats=0|1` returns
  `{known, findings[], summary{problems, suggestions, by_category{5}, by_rule}, checked{tables,
  columns, measures, relationships}, notes[]}`. The finding shape is described above.
- `POST /api/imported/{report_id}/model-health/fix` takes
  `{finding_id, base_hash, confirmed: bool}`. It answers with the usual edit receipt, or
  `409 {needs_confirmation: {kind: "model_health_fix", detail: fix_summary,
  outside_supported}}`.
- `GET /api/model-health/rules` returns `[{id, category, severity, title}]` (from `RULES`).

**Chat:**

- `model_health.is_health_request(message)` (phrases in `CHAT_TRIGGERS`: "check the model",
  "model health", "best practice", "BPA", "what's wrong with the model", ...) routes before the
  planner and the LLM to `service.model_health`. The reply is `model_health.describe(health)`.
- The LLM may only rephrase `describe`'s lines; its prompt gets messages only, never `fix` ops
  or formulas.
- "Fix the <n>th one" or "hide those columns" goes to the planner, which may propose
  `finding.fix` as the plan with the usual confirmation.

**Edit pipeline hook:**

- After every committed model or measure edit, compute health on the new head (cached, about
  0.1 s on Agents).
- Add to the receipt / SSE `model_health: {new: [finding dicts whose id was not in the health
  before the edit, restricted to findings_for(after, objects the batch touched)]}`. Never nag
  about old findings after an edit.

**UI:**

- A "Model health" pane in the imported editor, next to Fields and Format.
  - The summary chips show problems and suggestions per category.
  - Findings are grouped by category. Each shows a severity badge, the message, an expandable
    "why", clickable objects (select in the Fields pane) and a Fix button.
  - The Fix button opens a confirm dialog with `fix_summary`, "Needs Power BI Desktop", and an
    outside-supported note when `fix_outside_supported` is set.
  - Findings with `fix = null` get "How to fix", which shows `fix_summary`.
  - `notes` sit at the bottom, with a "Use data statistics" toggle (`stats=1`).
- Fields pane badges: `findings_for(health, [object])` gives a dot on the columns and measures
  that have findings.
