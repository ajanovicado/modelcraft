# Report tests on pinned numbers

Status: 2026-09-29, round 3 workstream `tests`. Module `src/ai_powerbi_analyst/studio/report_tests.py`,
tests `tests/unit/test_report_tests.py` (59, of which 2 on the real Agents dashboard, skipped when it is
absent). Not wired into the service, API, chat or UI yet: the wiring spec is at the end.

A Power BI developer knows a refactor did not change the answers because they test known numbers. A
report test is one number of the report (a measure, or a DAX expression the user typed) under a fixed
filter context, with what it should be. Tests are pinned from a card or a data point, or typed by the
user; they run on Studio's DAX engine or on Power BI Desktop's model; and a model or measure edit that
would change a number that passed before asks the user first.

Everything is deterministic code. The language model never computes a result; it may only phrase one.

## 1. A test

```json
{ "id": "t8d494d085bf3",
  "name": "Agents Avg MTD Sales",
  "measure": { "table": "# Measures", "name": "Employees Avg MTD Sales" },
  "expression": null,
  "filters": [
    { "table": "Top-Bottom-N", "column": "Top-Bottom-N", "op": "in", "values": [6.0],
      "date": false, "point": false, "measure": false },
    { "table": "Actuals-vs-PctChange", "column": "Top-Bottom-Field", "op": "in",
      "values": ["MOM% Sales"], "date": false, "point": false, "measure": false } ],
  "expect": { "equals": 73514.7960347328, "tolerance_abs": null, "tolerance_rel": null },
  "created_at": "2026-09-29T12:00:00+00:00",
  "source": "pinned",
  "format": "\\$#,0.00;(\\$#,0.00);\\$#,0.00",
  "origin": { "page": "ReportSection...", "visual": "2202e311b1493069e4eb" } }
```

- Exactly one of `measure` and `expression`. An `expression` is a DAX expression (not a query) the user
  typed (`source: "user"` only); it is parsed when the test is created and never sent to the LLM.
- `filters` all hold at once, in order. `op` is `in`, `not_in` or `between` (`[min, max]`, both ends
  included, `null` for an open end). `date`: the values are days `YYYY-MM-DD`. `point`: a category of a
  pinned data point (see 3). `measure`: `column` names a measure (a visual filter on a measure, kept for
  cards only).
- `expect` is one of `{equals, tolerance_abs?, tolerance_rel?}` (`equals: null` expects a blank; the
  larger tolerance applies; without tolerances `1e-9` absolute and relative absorb float noise between
  engines), `{between: [min, max]}`, `{not_blank: true}`. Text and true/false compare exactly.
- `format`: the number format the value is shown with (the field's own, else the model's), for plain
  answers. `origin`: where a pinned test came from (UI badges).
- `id` is derived from what is computed (`definition_id`: measure or expression + filters), so pinning
  the same number twice is refused as "already has a test". Ids never change afterwards (renames keep them).

`TestResult {id, name, status: passed|failed|not_checked, value, shown, expected, message, engine}`:
`value` is what the evaluator returned (null: blank or not computed), `shown` that value as the report
formats it ("46.76%", "$73,514.80", "18.91M"), `message` one plain sentence ("Agents Avg MTD Sales is
still $73,514.80.", "Total Sales is $935 now instead of $850.", "X was not checked: <why>.").

## 2. The store

`TestStore(state_root)` keeps `reports/<report id>/tests.json`:

```json
{ "version": 1, "tests": [ ...TestDefinition ],
  "last_run": { "revision": "r0007", "hash": "<head sha256>", "engine": "Studio's DAX engine",
                "ran_at": "...", "results": [ ...TestResult ] } }
```

Methods: `tests`, `last_run`, `add` (refuses a duplicate id and a 201st test), `replace` (same id),
`replace_all` (after `follow_renames`), `remove`, `record_run`. Every change is a whole-file atomic
replace (`report_revisions._write_atomic`: temporary file, fsync, `os.replace` with the Windows
retries) under a file lock (10 s). A report id must look like one (no path tricks) and its folder must
exist for a write. A damaged file raises `TestStoreError` and is never reset.

## 3. Pinning a number

`pin_from_visual(document, page_id, visual_id, point, *, value, report, measure=None, name=None,
now=None) -> TestDefinition` (raises `PinError` with a plain reason).

- The filter context is the renderer's: `live_report._page_context`, `_scope` and `_plan` give the
  visual's filters exactly as the canvas computes it (report filters, page filters, visual filters, the
  slicers that reach it through the page's interactions). Each compiled condition is read back into the
  test's filters. Order is kept: page, report, visual, slicers.
- `point` maps each category column of a chart or table (by the payload column's `query_ref`, or
  `Table.Column`) to the clicked row's value; empty for a card. It becomes `point` filters.
- A filter on a measure is kept for a card (it decides whether the card shows anything) and dropped for
  a data point (it only decides which rows a visual shows; the Agents table's rank filter is one).
- `measure` picks the number when the visual shows several (payload column `query_ref` or name). A measure
  the formatting adds (a card's reference label) is found in the model and computed under the same filters.
- `document` (the caller's `PbirDocument`, optional) is checked to still have the visual; the filters are
  read from the head (`report`), where the number was computed.
- Refused, with the reason: content visuals and slicers, a visual Studio cannot compute (calculation
  groups, field parameters, filters that need Power BI), a missing point, an ambiguous measure, a
  category picked as the number, and filters a test cannot hold: `||` across values and strict `<`/`>`
  (only lists and ranges that include their ends). A date category must be a whole day.

`suggest_tests(document, payloads, *, report, limit=8, now=None)` proposes (never stores) tests from
`render_page` payloads: each card's number (big cards first, then top to bottom, left to right), then
the numbers its reference labels show (a measure whose formula is a bare number, a target, is skipped),
then, when fewer than 5, the first row of each chart or table. Hidden visuals, visuals Studio could not
draw, blanks and pictures are skipped.

## 4. The query and the evaluators

`query_of(test)` builds the visual's own query shape with `live_report._grouped`: each column filter
as `KEEPFILTERS(FILTER(ALL(table), condition))` around the value, a measure filter as a row filter, in
the stored order; the value is `SELECTCOLUMNS(..., "Value", [M0])`. A data point is its row of the
visual's grouping, `FILTER(SUMMARIZECOLUMNS(<point columns>, "M0", [Measure]), <point values>)`, not the
measure under a filter on the category: ALLSELECTED reads those differently (in the test fixture a store
ranks 2nd as a row of the table and 1st under a filter on itself). Measures are named bare
(`[Total Sales]`): names are unique and a measure moved to another table is the same number.

`run_tests(tests, evaluate)` calls `evaluate.prepare(tests)` when it exists, then `evaluate(test)` per
test. Outcomes:

| evaluator raises | status |
|---|---|
| `NotComputable`, `DaxUnsupported` (incl. `DaxUnavailable`, `DaxModelIncomplete`), `DaxEngineLimit`, a formula Studio's parser cannot read, any other unexpected error | `not_checked`, with the reason |
| `TestBroken` (a measure or filter column gone; an error Power BI reports) or a `DaxEngineError` (fails as it would in Power BI) | `failed` |
| a value | compared with `expect` |

- `LocalEvaluator(report, snapshot | model=...)`: the head-aware model of `report_engine`; one engine
  per run, so shared measures are computed once (Agents' 8 suggestions: 4.6 s to 2.3 s), each test with
  its own 30 s budget. `unavailable_reference` refusals are `not_checked`; names the model no longer has
  are `failed` in the user's words.
- `QueryEvaluator(run)`: `run({id: query}) -> {id: {ok, rows | error}}`, exactly the shape of
  `desktop_model.query(runner, server, queries)` (Power BI Desktop's model through the TOM runner); one
  call for all tests. A per-query error fails the test ("Power BI reports an error in this number");
  the call failing (Desktop not answering) makes every test `not_checked`. For a numeric test, a decimal
  the runner prints as text is read back as a number.

## 5. Gating an edit

`gate(before, after)`: the `after` results of tests that passed before and fail after. A test not
checked on either side, or already failing, never blocks. `check_gate(before, after, confirmed=False)`
raises `ReportTestsBroken` (a `report_edit_context.NeedsConfirmation`, `kind = "report_tests_broken"`,
`detail` = the plain text, `results` = the broken tests) unless confirmed, and returns the broken tests.

`projected_report(report, operations)`: the report with the batch's measure operations applied to its
measures (create, update, rename, delete; report-layer operations ignored), or `None` when a model-family
operation is in the batch (only Power BI's saved model can say what that does).
`follow_renames(tests, operations)`: tests with renamed measures, columns and tables written the new way
(ids kept). Run the "after" side on the renamed tests.

`describe(test)`, `describe_results(results)`, `confirmation_text(results)`, `shown_value(value, fmt)`
give the plain words.

## 6. Measured on the Agents dashboard (read-only copy, local engine)

`suggest_tests` proposes 8 tests; all pass (filters: Top-Bottom-N is 6, Top-Bottom-Field is MOM% Sales,
the two slicers' saved selections, which reach every visual):

| Test | Measure | Value | Shown |
|---|---|---|---|
| Agents with Positive Change % | Employees with Positive Change % | 0.467576791808874 | 46.76% |
| Agents Avg MTD Sales | Employees Avg MTD Sales | 73514.7960347328 | $73,514.80 |
| Agents Avg MTD % Change | Employees Avg MTD % Change | 0.353055854189281 | 35.31% |
| Number of Agents Sold above 100K | Number of Employees selling more than 100K MTD | 19 | 19 |
| TopN/BottomN | dummy | 18914632.251 | 18.91M |
| Agents with Positive Change %, PM | Number of Employees with Positive change PM | 0.37542662116041 | 37.54% |
| Agents Avg MTD Sales, PM | Employees Avg MTD Sales PM | 72193.2528664122 | 72,193.3 |
| Agents Avg MTD % Change, PM | Employees Avg MTD % change PM | 0.243847369031617 | 0.243847 |

The four targets (80000, 0.45, 20, 0.55) are skipped as constants. Data points also pin and pass:
"TopN/BottomN/ MTD Sales for Others" = 18998521.5511 (19.00M) and the table row EmployeeKey 247
"vs Previous Month" = 7.13320204322304 (713.32%). Doubling [Total Sales] (projected, before Desktop)
breaks the card ("Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80.") and the Others bar,
and leaves the MoM ratio of row 247 passing. A run of the 8 suggestions takes about 2.3 to 3 s.

## 7. Unverified and limits

- `QueryEvaluator` was tested with fakes only: the runner's text for a decimal (currency, fixed
  decimal) under a Windows culture with a decimal comma, and that Desktop answers the generated queries
  exactly as the local engine does, need a live check (run `QueryEvaluator(partial(desktop_model.query,
  runner, server))` on the 8 Agents suggestions and compare with section 6).
- Pinning reads `live_report._page_context`, `_scope`, `_plan` and `_grouped` (private; no public API
  gives a visual's compiled filters). A change to their signatures breaks pinning; the tests catch it.
- Strict comparisons, `||` across values, relative-date and Top N filters cannot be pinned (the latter
  two already need Power BI for the canvas). A test holds the filter context at pin time: a later change
  of a slicer's saved selection does not move the test (that is the point of a pinned number).
- Tests that read `TODAY()`/`NOW()` move with the clock, as they do in Power BI.

## 8. Wiring spec (for the integration agent)

### Service methods (`ImportedEditingService`; it has `state_root`, the head report, its snapshot)

Store: `self.tests = report_tests.TestStore(self.state_root)`. All calls take the report lane lock the
other report reads take.

- `report_tests(report) -> {"tests": [TestDefinition], "last_run": TestRun | None, "stale": bool}`;
  `stale` = `last_run.hash != head hash`.
- `pin_report_test(report, page_id, visual_id, point, value, measure=None, name=None) -> {"test",
  "result"}`: `pin_from_visual(document_of_head_or_None, ...)`, then `run_tests([test],
  LocalEvaluator(report, snapshot))`; store it (`add`) whatever the result, and return the result so the
  UI can say "Studio calculates 850 here; the card showed 851" when it is not `passed`. `PinError` and
  `TestStoreError` -> 422 with the message.
- `add_report_test(report, body) -> TestDefinition`: `new_user_test(name, expect=..., measure=... |
  expression=..., filters=..., fmt=...)`; `ValidationError` -> 422 with its first message.
- `update_report_test(report, test_id, body) -> TestDefinition` (`name`, `expect`, `filters` only; the
  id stays) via `store.replace`; `delete_report_test(report, test_id) -> bool`.
- `run_report_tests(report, engine: Literal["local", "desktop", "auto"] = "auto") -> TestRun`: local
  first; with `auto`, the tests the local engine left `not_checked` are run again with
  `QueryEvaluator(partial(desktop_model.query, runner, server))` when Desktop has the report open and the
  runner is available (never opens Desktop for this). `store.record_run(report.id, TestRun(revision=
  head.id, hash=head.blob, engine=..., ran_at=now_text(), results=...))`.
- `suggest_report_tests(report, page_ids=None) -> list[TestDefinition]`: render each page with the local
  engine (the cached payload when the canvas has one) and call `suggest_tests(None, payloads,
  report=report)`; nothing is stored until the user accepts (then `add` each).

### API routes (JSON; ids in the path; 409 for a stale `revision` like the render routes)

| Method | Path | Request | Response |
|---|---|---|---|
| GET | `/imported/{workspace}/tests` | - | `{tests, last_run, stale}` |
| POST | `/imported/{workspace}/tests/pin` | `{page_id, visual_id, point: {query_ref: value}, value, measure?, name?}` | `{test, result}` |
| POST | `/imported/{workspace}/tests` | `{name, measure?: {table, name}, expression?, filters: [...], expect, format?}` | `TestDefinition` |
| PATCH | `/imported/{workspace}/tests/{test_id}` | `{name?, expect?, filters?}` | `TestDefinition` |
| DELETE | `/imported/{workspace}/tests/{test_id}` | - | `{removed: bool}` |
| POST | `/imported/{workspace}/tests/run` | `{engine?: "local"\|"desktop"\|"auto"}` | `TestRun` + `{text: describe_results(results)}` |
| GET | `/imported/{workspace}/tests/suggestions` | `?page=<id>` (repeatable) | `[TestDefinition]` + `[describe(t)]` |

### Edit-pipeline gate (`ReportEditor`, with `tests: Callable[[ImportedReport], list[TestDefinition]]`
and `confirmed: set[str]` passed in by the service; `"report_tests_broken" in confirmed` confirms)

Only batches whose `outcome.model_operations` is non-empty are gated (a report-layer edit changes no
number). The gate never runs when the report has no tests.

1. **Before Desktop is touched** (`apply`, right after the first `self._dry_run(...)` succeeds and
   before `self._begin(...)`; the lock is held, nothing is written):
   - `tests = tests_of(report)`; `before` = `store.last_run` results when `last_run.hash == head.blob`,
     else `run_tests(tests, LocalEvaluator(report, snapshot))` (recorded).
   - `projected = projected_report(report, outcome.model_operations)`; when not None:
     `after = run_tests(follow_renames(tests, outcome.model_operations), LocalEvaluator(projected,
     snapshot))`; `check_gate(before, after, confirmed=...)`. `ReportTestsBroken` propagates like any
     `NeedsConfirmation` (the dry run's own handler already re-raises those untouched): nothing written,
     Desktop never opened.
   - When `projected` is None (model-family operations), go on to step 2.
2. **After Power BI saved, before the revision is committed** (`_model_transaction`, after
   `staged, summaries = self._check_model_layer(...)` and before `store.commit(...)`): build the saved
   model's view `saved_report = report.model_copy(update={"measures": self._read_measures(provisional)})`
   plus the saved facts (`with_model_facts(saved_report, facts_of(provisional))`, the facts
   `_check_saved_facts` already read), `after = run_tests(follow_renames(tests, operations),
   LocalEvaluator(saved_report, snapshot))` and `check_gate(before, after, confirmed=...)` for the tests
   step 1 did not decide (all of them when step 1 was skipped). A raise here goes through `apply`'s
   `except Exception: self._abandon(batch); self._fail(batch, error)`: the head is put back over
   Desktop's working copy and nothing is committed. The user's "yes" re-runs the edit with the flag.
3. **After commit** (`_finish` path, in the service): `store.replace_all(report.id, follow_renames(tests,
   operations))` when a rename was in the batch; `store.record_run` with the `after` results and the new
   head; the broken tests the user confirmed go into the receipt's `warnings` as
   `describe_results(broken)` (they stay stored, failing, until the user removes or re-pins them).

`NeedsConfirmation` shape for the client (same envelope as `replace_conditional`):

```json
{ "status": "needs_confirmation", "kind": "report_tests_broken",
  "message": "This change would break 2 report tests:\n- Agents Avg MTD Sales is $147,029.59 now instead of $73,514.80.\n- TopN/BottomN/ MTD Sales for Others is 38.00M now instead of 19.00M.\nApply it anyway?",
  "tests": [ ...TestResult of the broken tests ],
  "confirm": { "confirmed": ["report_tests_broken"] } }
```

Chat: the chat edit pipeline shows `message` with Apply anyway / Cancel; Apply re-sends the batch
with `confirmed: ["report_tests_broken"]`.

### Chat triggers (deterministic; before the LLM)

- "run the tests", "check my numbers", "did anything change", "are the numbers still right" ->
  `run_report_tests(report)` -> reply `describe_results(results)`.
- "pin this number", "test this number", "remember this value" with a selected visual (and data point)
  -> `pin_report_test` -> reply `describe(test)` + the result's message.
- "what tests do I have", "list the tests" -> one `describe(test)` line per test.
- "suggest tests" -> `suggest_report_tests` -> offered as cards with Add.

### UI surfaces

- A **Tests** pane (beside History): the list with status badges (passed, failed, not checked with the
  reason on hover), Run, Add (measure or DAX expression, filters, expectation), edit and delete; a
  "stale" note when `last_run.hash` is not the head's.
- Canvas: a small pin action on a card and on a data point's context menu ("Pin this number as a
  test"); a badge on visuals with tests (`origin.visual`), red when one of them fails.
- Edit receipts: "2 report tests still pass" or the confirmed broken tests.
- The confirmation dialog of `report_tests_broken` (above).

### Requests to other owners

- Renderer (`live_report.py`): please keep `_page_context`, `_scope`, `_plan`, `_grouped` and
  `PageContext.formats` stable, or expose a public `visual_filters(report, page_id, visual_id) ->
  (fields, predicates)` and `grouped_query(...)`; `report_tests` would switch to them.
- Desktop session: a `DesktopPort.query_model(instance, queries)` wrapper of `desktop_model.query` so
  the service can build a `QueryEvaluator` without reaching into the runner.
