# Imported report review ("look at what I just did")

Status: 2026-10-02 (styling edits removed). Module: `src/ai_powerbi_analyst/studio/report_review.py`.
Tests: `tests/unit/test_report_review.py` (30, including the structure twin and the real Agents dashboard,
read-only, skipped when absent). Not wired yet: the integration agent wires it from the spec at the end.

A developer glances at the canvas after every change. The local model cannot see, so Studio checks the page
itself, with deterministic code (no model call makes or words a finding), and says what a developer would
notice. The model may only choose which finding to mention; it never computes one.

## API

```python
review_page(document: PbirDocument, page_id: str, *, payload: Mapping | None = None,
            focus: Iterable[str] | None = None) -> Review
review_edit(before: Review, after: Review) -> list[Finding]
describe_findings(findings, limit=3) -> str          # a short paragraph for a chat reply or receipt
text_width(text, size_px, family=None, bold=False) -> float
RULES: dict[str, str]                                # stable rule id -> plain name
```

- `page_id`: page id, display name or 1-based position (`PbirDocument.resolve_page`).
- `payload`: the page's render payload of the same head (`live_report.render_page`: rows, errors, formats;
  or `render_formatting_payload`: tokens, errors, formats, no rows). A payload of another page is ignored,
  and a payload row whose `visual_type` differs from the document (drawn before a type change) is ignored.
  Without a payload the tokens are resolved from the document through the renderer's own functions in
  `report_tokens` (the theme is merged the way `report_theme.merged_theme` merges it), so the result is
  the same for everything the tokens decide; rules that need what only the renderer knows are listed in
  `skipped` with the reason.
- `focus`: the visual ids an edit touched. They are what a fix moves ("snap the edited visual to its
  neighbours"), and their findings come first.
- Pure and fast: ~30 ms on the Agents page from the document alone, ~10 ms with a payload (budget 200 ms).
  Deterministic order: findings about focused visuals, then problems before suggestions, then rule order,
  then reading order (top to bottom, left to right).

`Review` (`to_dict()` is what the API sends):

```json
{ "page": "ReportSection...", "page_name": "1",
  "findings": [ Finding ],
  "summary": { "problems": 1, "suggestions": 4, "by_rule": { "same_field_slicers": 1, "near_miss": 4 } },
  "checked": ["off_canvas", "overlap", "..."],
  "skipped": { "empty_visual": "Needs the page's data (open the page first)." },
  "notes": [],
  "focus": [] }
```

`Finding`:

```json
{ "id": "near_miss-3f2a9c01be", "rule": "near_miss", "severity": "suggestion",
  "visuals": ["b404a461ede7604a2774", "97d1504080b699eae7d7"], "page": "ReportSection...",
  "message": "The 'Total MTD Sales' stacked column chart is 1.4 px out of line with the 'Avg Discount per Transaction' stacked column chart below it.",
  "fix": [{ "op": "set_position", "visual": "b404a461ede7604a2774", "x": 172.97297297297297 }],
  "fix_summary": "Move it 1.4 px so their left edges line up." }
```

- `visuals`: the visual ids (the one a fix changes first); `[]` for a page-wide finding.
- `message`: one plain sentence; visuals are named as a reader sees them (`report_edit_geometry.describe`),
  never by id; sizes in points, positions in page pixels.
- `fix`: operation dicts, or `null` when the right fix is a judgement call (which of two overlapping visuals
  to move, which slicer is wrong). Every fix is parsed before it is offered: `report_edits.parse_operations`,
  else the family's own parser (`report_edit_conditional.parse_conditional` for `set_number_format`, which
  the facade does not register yet). A fix that does not parse is dropped, never offered. Fixes are never
  applied by the review; the user applies them like any other edit (one revision, undoable).
- `id` = rule + a hash of (page, key); stable across runs. The key (rule, sorted visuals, what of them) and
  `amount` (how bad, in the rule's unit) are internal and drive `review_edit`.

`review_edit(before, after)`: the findings of `after` whose key `before` did not have, or that the edit
made worse by at least the rule's step (`_WORSE`: 5 % more overlap, 0.5 px of
misalignment, 1 more slice/visual...). Old issues are never repeated after an edit; "review this
page" lists them.

## Rules

| id | severity | needs | what | fix |
|---|---|---|---|---|
| `visual_error` | problem | payload | the renderer's `error` (not `needs_desktop`) | none |
| `empty_visual` | problem | rows | a chart, card, table or slicer with no rows, or only blank measures (a card whose number is off is a label and is skipped) | none |
| `off_canvas` | problem | - | wholly off the page, or cut off by an edge (1 px slack, the validation's `_EDGE_TOLERANCE`) | `set_position` back inside |
| `overlap` | problem | - | the validation's advisory test (a quarter of the smaller box covered), run through `report_edit_validation._check_overlap`; text boxes, shapes, pictures, buttons (its `_FRAME_TYPES`), hidden visuals and a caption laid over its host (`report_captions.find_caption_overlays`) are left out | none |
| `too_small` | problem / suggestion | - | a data visual under the validation's `_MIN_SIZE` (20 px); a chart under 150 x 100 px; a card shorter than its number and caption | `set_position` (Power BI's default size, the minimum, or the height the text needs) |
| `same_field_slicers` | problem when a header names another field, else suggestion | - | two visible slicers on one page over one field | none |
| `near_miss` | suggestion | - | neighbours in a row or column (sharing 25 % of the smaller side, nothing between) whose top/left edges are 1 to 6 px apart, or whose bottom/right edges are when the other edge lines up (a size difference) | `set_position` of the one to change: the edited visual, else the one whose value fewer visuals on the page share, else the later one |
| `uneven_gaps` | suggestion | - | 3+ visuals lined up in a row/column whose gaps differ by 1 to 24 px | `distribute_visuals` |
| `mixed_number_formats` | suggestion | payload | one measure (query ref) shown with different format / display units / decimals on the page | `set_number_format format=` the majority's, when the majority is a plain format string |
| `too_many_visuals` | suggestion | - | more than 20 visible data visuals | none |
| `pie_slices` | suggestion | rows | pie or donut with more than 8 categories (or truncated rows) | none |
| `needs_power_bi` | suggestion | payload | `unsupported_reason` or `needs_desktop` from the renderer | none |

Styling-only findings (font consistency, contrast, titles, legends and colors) are removed. Existing
styling remains available to the renderer and to structural checks that need the visual's geometry.

Composites: a text-only visual laid over another (the Agents "TopN/BottomN" card over the Top-Bottom-N
slicer) is one unit for the layout rules (its box is the union), and a fix never moves half of it.

## Thresholds and why

All are module constants with their reason in a comment: `NEAR_MISS_MIN = 1` (below is Desktop's float noise:
positions carry 14-17 digits and a rescaled page leaves 0.3-0.6 px differences; the Agents grid has two),
`NEAR_MISS_MAX = 6` (further looks deliberate), `NEIGHBOUR_SHARE = 0.25`, `UNEVEN_GAP_MAX = 24` (bigger
differences separate groups), `CHART_MIN_WIDTH/HEIGHT = 150/100` (design threshold, not measured),
`DEFAULT_PADDING = 5`, `LINE_HEIGHT = 1.2`, `MAX_VISUALS = 20` (every data visual sends a query; the
Agents page has 15), `MAX_SLICES = 8`, `MAX_LAYOUT_VISUALS = 120`.

## Measured on the Agents dashboard (Desktop-authored, page "1", 15 visuals)

Same result from the document alone, with the formatting payload, and with the full `render_page` payload of
Studio's DAX engine (real rows, imported into a throw-away state): **5 findings, 1 problem, 4 suggestions.**

| rule | count | finding |
|---|---|---|
| `same_field_slicers` | 1 | "The 'Country' slicer filters Channel, the same field as the slicer of Channel, although its header says 'Country'." (problem, no fix) |
| `near_miss` | 4 | Channel slicer 61 px tall next to two 60 px slicers (fix: height 60); 'Total MTD Sales' chart 1.4 px left of the grid (fix: x = 172.97); 'Avg MTD Sales by Agents' chart 1.4 px taller and 1.4 px wider than its neighbours (fixes: height 224.32, width 360.81) |
| every other rule | 0 | - |

Deliberately not reported (the noise the tuning removed): the "TopN/BottomN" label card over the Top-Bottom-N
slicer (a caption overlay, not an overlap; it also names the slicer, so no missing title), the slicer's top
5 px below the table's (it is the composite's top that lines up, and it does), the 5 px bottom difference of
the two left-panel slicers whose tops are 25 px apart (not meant to line up), the 0.6 and 0.3 px differences
of the lower charts, the table without a title, and the contrast of the ten see-through visuals over the page
picture (not guessed; one note says so). All four `near_miss` fixes and none of the others were applied to
the twin through `report_edits.apply_operations`: each clears its finding and introduces nothing new.

## Unverified / limits

- Text widths used for structural card sizing are estimates, not measurements.
- `find_caption_overlays` reads stored positions, which are relative for group members.
- The `set_number_format` fix parses with the conditional family's parser until the facade registers it.
