# Investigating a number on an imported report

Status: 2026-09-29 (round 3, workstream `explain`). Module `src/ai_powerbi_analyst/studio/number_explain.py`,
tests `tests/unit/test_number_explain.py`. Not wired yet: the integration follows the wiring spec at the end.

"Why does this card show 46.76%?", "why is this bar red?", "why is this cell blank?" are answered the way a
Power BI developer investigates: open the measure, evaluate what it uses in the same filter context, check
which filters and slicers reach the visual and which relationships carry them, look at why a mark has its
colour. No LLM call produces any of it; the local model may only be skipped.

## What it computes

`explain_point(report, page_id, visual_id, point=None, *, engine, payload=None, request=None, focus="value")
-> Explanation`

- `report` is the `ImportedReport` (the brief calls it `document`; the renderer, the engine and the measures
  all work from the report, so that is what it takes). `engine` is the live report's engine:
  `LocalReportEngine(report_model(report, snapshot))` or Power BI Desktop's `NativeReportPreview`.
- `point` is `{"values": {<query_ref of a category column>: value}, "measure": <query_ref or name>}` (or a
  `Point`); None for a card, or for a chart or table as a whole. A column is matched by its `query_ref`, by
  `Table.Name`, its name or its label: Desktop keeps a projection's `queryRef` across a rename, so the
  Agents table's `[vs Previous Month]` has queryRef `# Measures.vs PM`.
- `payload` is the page (or visual) render the user sees; without it the visual is rendered here once.
  `request` is the canvas state (`LiveReportRequest`: selections, searches, bookmark, drill).
- `focus` (`value`, `colour`, `blank`) orders the text; the payload is the same.

Every value comes from **one query** built with `live_report`'s own pieces: `_compile` (the visual's fields
and query plan), `_grouped` (the visual's grouping and filters, measure filters included) and, for a point,
`FILTER` over the grouped result on the point's category values. Filtering the result instead of adding
the point as a filter keeps `ALLSELECTED` exact (an outer filter would become the "selected" state). Each
step is an extra column of that query, so it is evaluated in exactly the point's filter context. If the
engine refuses the query (`DaxUnsupported`, `DaxUnavailable`, an engine error), the value and each step are
asked alone (40 s budget) and a refused part keeps its reason; the rest is kept. Nothing is estimated: a
ratio sentence is written only when both parts were computed.

`Explanation` (`to_dict()` is the JSON payload):

| Field | Meaning |
|---|---|
| `page_id`, `visual_id`, `visual`, `kind` | The visual and its caption (a card's label text) |
| `point` | `{column label: value}` of the point (empty for a card or a whole visual) |
| `measure`, `formula`, `meaning` | The measure, its own DAX (for the UI, never in `text`), a plain sentence (`dax_describe`) |
| `value`, `formatted`, `computed` | The engine's value, as the canvas formats it (payload text when it matches, else the format string) |
| `period` | "for December 2009, month to date (through 31 Dec 2009), the last date in the data" (card_answers `_period`) |
| `steps` | `Step {name, kind, owner, formula, meaning, value, formatted, computed, usage, depth, reason}` |
| `filters` | `FilterUse {source, field, condition, origin, on_measure, reaches, how}` |
| `relationships` | `JoinPath {tables, joins, why}` |
| `formatting` | `ColourReason {target, color, name, rule, why, measure}` or null |
| `blank` | Why the value is blank, or null |
| `notes`, `engine`, `text` | Plain notes, who computed it, the chat answer |

### Steps

- `variable`: each scalar `VAR` of the measure (table variables have no single value and are skipped; the
  variable the measure returns is the value itself). A variable the measure can return takes the measure's
  format only when it is the same kind of value as the one returned here (`_Avg_MTD_Asc/_Desc/_Others` all
  come from the same measure; `_Result_2` holds `[MTD Total Sales]` while `_Result_1` holds a percent).
- `condition`: every `IF` condition and `SWITCH(TRUE(), ...)` case down the chain (4 levels), so the text
  says which branch was taken and why ("the first SWITCH condition that holds is ... = 3, so it returns
  _Avg_MTD_Asc = -86.82%").
- `part`: numerator/denominator of a `DIVIDE` or operands of arithmetic when they are expressions.
- `measure`: every measure the formula calls, with `usage`:
  - `direct`: used in the point's own context. Valued, and followed (depth 2) with its own steps.
  - `per_row` (inside `FILTER`, `SUMX`, `RANKX`, `TOPN`...) and `shifted` (inside `CALCULATE` with its own
    filters, `TOTALxTD`): **not valued**. The formula works it out for every row, or under other filters,
    so a single number "for this point" would be a number the formula never used. The text says where it
    is worked out ("for every DimEmployee[EmployeeKey] (inside FILTER)"). This refines the brief's "each
    evaluated in the same filter context": those that are used in that context are.
- hidden probes (in `text`, not in `steps`): rows an iterator checks (`COUNTX(FILTER(T, ...))`: "137 of the
  294 values it checks"), an `AVERAGEX`'s rows, non-blank rows and sum ("$73,514.80 = $19,260,876.56 ÷
  262"), the period's end and last data date, and the rows of the fact tables it adds up (shown for a plain
  sum or count, and used for blank answers).

### Filters

The visual's query plan (`_plan`) is traced back to where each predicate comes from, by recompiling each
source with the same `Compiler`: `report`, `page`, `visual` (filter objects, worded with
`report_filters.describe_filter`), `slicer` and `cross_filter` (`_scope` contexts through the page's
interactions), `drill` (`_drill_predicates`), `bookmark` (a filter state or slicer the applied bookmark
saved), plus `point` (the point's own category values). A predicate that matches no source is `other`
(should not happen; kept visible). A highlight is not a filter: noted instead.

`reaches` says whether the filter can change this number: true when the measure (or anything it calls)
reads the filtered table, or an active relationship path takes the filter to a table it reads (the path is
named); false when neither, worded "it does not change this number"; null when the measure builds joins of
its own (`TREATAS`, `USERELATIONSHIP`, `CROSSFILTER`) or the table is unknown. A filter on a measure
(`on_measure`) decides which rows a visual shows, not the row's value (a card: whether it shows a value).

Relationships come from `report_relationships(report)` (what the engine uses), so they work with Desktop
too. `relationships` lists the paths filters take to the tables the measure reads, and the paths inside the
measure (the table it adds up to the others it reads: FactSales → DimStore → DimEmployee).

### Blank values

`blank` names, in order: the measure's own `IF`/`SWITCH` branch that returns BLANK (or an IF without else,
a SWITCH without default) with the condition that led there; a blank variable or measure the chain returns;
a `DIVIDE` whose denominator is 0 or blank; no rows of the fact tables for the point's filters; a grouping
that does not reach the tables read; and, for a (Blank) category, how many fact rows have keys the dimension
lacks (from the local model).

### Colour

Resolved as the render contract draws it (sections 3, 12): a chart point's `series.ruleColors` (a rule over
its measure), `categoryColors` (a fixed colour for that category: the Agents BottomN bars), `seriesColors`,
`defaultColor`, then the theme's data colours; a table cell's `backgroundIndex`/`colorIndex`; a card's
`cardValue.colorIndex`. A field-value colour (a measure returns it) is explained by planning that measure's
steps into the same query (the Agents table's `[CF Table]` returns "white" for the "⋮" separator rows); a
gradient or rules colour by the rule `render_rules` evaluated, which case holds for the row's input, or the
scale's stops and the lowest/highest value shown (noted as possibly different when only 100 rows were read).
`colour_name` gives a plain name (#DC143C: red) so chat can match "why is this bar red?". With
`focus="colour"` on a table, a row whose asked column has no rule takes the first column of the row that has
one.

## Chat

`CHAT_TRIGGERS` (regex strings; `is_explain_question(message)`): "why does/is/are ... <number, %, blank,
colour word, 'this bar/cell/card...', 'so high', 'higher'>", "how is ... calculated/computed/worked out",
"explain this/the ... number/value/bar/row/cell/card/colour", "where does ... come from", "what makes ...
<colour/blank/so high>". Not triggered: "why is my model slow", "make the bars red", "why don't you ...",
"explain the data model" (`model_answers.model_walkthrough` keeps that).

`answer(message, AskContext) -> str | None` and `answer_explanation(...) -> Explanation | str | None`
(the string is a question back). `AskContext(report, engine, page_id=None, visual_id=None, point=None,
request=None, payload=None)`. Target resolution (`find_target`), from the page payload (rendered here when
not given; only the named visual when one is named):

1. the selected visual (`visual_id`, and `point` when the canvas sent one);
2. a visual named by caption, title or measure name (longest match wins);
3. category values named in the message ("the BottomN bar", "agent 201");
4. numbers the message names, matched to the formatted text or to the raw value within the message's
   precision ("46.76%", "73.5K", "19M"); numbers inside visual names are ignored ("above 100K");
5. colour words (the point's colour's name) and "blank/empty/missing" (blank values).

One card, or one row/bar of one visual, left: explained. Several: a question back listing up to six ("Several
numbers on this page fit that question: ... Select one on the canvas ... and ask again."). A number no visual
shows: "No visual on this page shows 50% right now ...". Nothing on the page named: None (the model answers).

`text` never contains a measure's whole formula (it stays in `formula` for the Explain panel); a condition
with no plain wording is shown as a DAX snippet of at most 120 characters.

## Measured on the Agents dashboard (read-only import, local engine)

| Question | Answer (abridged) | Time |
|---|---|---|
| Card "Agents with Positive Change %" | 46.76% = 137 ÷ 293; 137 of the 294 DimEmployee[EmployeeKey] values checked (one is the (Blank) member); both slicers "do not change this number"; FactSales → DimStore → DimEmployee | 1.3 s with payload, 8 s rendering |
| BottomN bar of "Avg MTD % change by Agents", why red | -86.82%; SWITCH case Order = 3 → _Avg_MTD_Asc; #DC143C (red), fixed colour for BottomN | 3-7 s |
| Table row 201, [vs Previous Month] blank | returns BLANK by its own IF: _Rank_Middle_Empty_Bar = 7 is _N + 1 (N = 6) | 4-9 s |
| Table row 201, why white | [CF Table] returns "white" (same rank test) | 3.5 s |
| Others bar of "Total MTD Sales" | 18,998,521.55 = _Avg_MTD_Others; the text shows its `[Rank MTD Desc] is more than _N` condition twice (the DEV-05 duplicate) | 5.6 s |
| Card "Agents Avg MTD Sales" | $73,514.80 = $19,260,876.56 ÷ 262 of 294 agents with a value | 1.4 s |

## Unverified / limits

- Values are the engine's; on the local engine they match Desktop where the engine does (the renderer's
  existing guarantee). Desktop-engine value shapes (dates as text) are normalised but were not run.
- A variable the measure returns is formatted with the measure's format only for the same kind of value;
  other variables and parts use plain formatting.
- `ALL(T[K])` counts include the (Blank) member Power BI adds; the text says so from the local model's
  blank-row flags (not available on Desktop).
- Matrix, custom and unsupported visuals: no value (noted). RLS is not applied (noted when the model has
  roles, as the renderer does).

## Wiring spec (for the integration agent)

Service (`service.py`):

```python
def explain_number(self, workspace_id: str, request: ExplainNumberRequest) -> dict[str, Any]:
    """POST /api/workspaces/{id}/imported-report/explain"""
    workspace = self.workspace(workspace_id)
    report = self.imported_report(workspace.id)
    snapshot = self.snapshot(workspace.active_snapshot_id)
    engine = LocalReportEngine(report_model(report, snapshot))   # DaxUnsupported/DaxModelIncomplete
    # -> fall back to the Desktop engine the live report uses (the same choice as render_page).
    found = number_explain.explain_point(report, request.page_id, request.visual_id, request.point,
                                         engine=engine, request=request.canvas, focus=request.focus)
    self.store.event(workspace.id, "report.explain", {"visual_id": request.visual_id})
    return found.to_dict()
```

Contract (`contracts.py`):

```python
class ExplainPoint(ClosedModel):
    values: dict[str, StrictStr | StrictInt | StrictFloat | StrictBool | None] = Field(max_length=12)
    measure: str | None = Field(default=None, max_length=300)

class ExplainNumberRequest(ClosedModel):
    page_id: str = Field(min_length=1, max_length=200)
    visual_id: str = Field(min_length=1, max_length=200)
    point: ExplainPoint | None = None
    canvas: LiveReportRequest | None = None      # the request the canvas rendered with
    focus: Literal["value", "colour", "blank"] = "value"
```

API (`api.py`): `POST /api/workspaces/{workspace_id}/imported-report/explain`, body `ExplainNumberRequest`,
200 with `Explanation.to_dict()`; `LookupError` → 404 ("This visual is not on this page"), `ValueError`
(malformed point, bad page) → 422 with the message. Runs on the report lane (read-only, no lock beyond
reading the head).

Chat (`service.py`, `_imported_card_answer` and the chat entry at "A selected card"): before
`what_if_answer`/`card_answer` and before the LLM, when `number_explain.is_explain_question(message)`:

```python
context = number_explain.AskContext(report, engine, page_id=request.context.page_id,
                                    visual_id=request.context.visual_id)
found = number_explain.answer_explanation(message, context)
if isinstance(found, number_explain.Explanation):
    yield {"event": "explanation", "data": found.to_dict()}   # the UI shows formula + steps locally
    text = found.text
elif isinstance(found, str):
    text = found                                              # a question back
# None: continue with card_answer / the model as today
```

Route order for an imported report: edits (`imported_edit_events`) → `number_explain` → `what_if_answer` →
`card_answer` → `model_walkthrough` → data answers → the model. `card_answer` stays for "what does this card
show" (not a trigger). Store only `text` in the chat history; never put `formula` into an LLM prompt.

UI: an "Explain this number" item in the context menu of a card value, a chart point and a table cell,
sending `{page_id, visual_id, point: {values: {<category query_ref>: <row value>}, measure: <column
query_ref>}, canvas: <current LiveReportRequest>}`; an Explain panel (or a card in chat for the
`explanation` SSE event) showing the headline, `formula` (monospace, local only), the `steps` tree (indent
by `depth`, grey "no single value here" for `per_row`/`shifted`, a warning icon with `reason` when
`computed` is false), `filters` with a source badge and a "does not change this number" style when
`reaches` is false, `relationships` as chains, the colour swatch from `formatting`, and `notes`.

Requests to other owners:
1. `live_report`: public, stable names for what this module imports privately (`_compile`, `_grouped`,
   `_scope`, `_unbound`, `_drill_predicates`, `_date_literal`), e.g. `compile_visual`, `grouped_query`,
   `page_scope`; this module would switch to them.
2. `card_answers`: make `_period`, `_date_extras` and `_card_label` public (`card_period`,
   `period_probes`, `card_label`); `dax_describe`: a public `describer(measures, variables)` instead of
   `_Describer`; `render_rules`: public `holds` for `_holds`.
3. The real-file tests import the Agents PBIX once per module (about 30 s) and render its page for the
   chat test: about 90 s in all, skipped when the file is absent.
