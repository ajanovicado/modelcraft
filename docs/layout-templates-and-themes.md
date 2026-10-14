# Layout templates and report themes

New drafts can now use one of several reviewed page structures and one of five
report themes. Qwen may *name* a template and a theme in the existing narrative
call; it never places visuals, binds fields or writes theme JSON.

## How a draft chooses

| Order | Layout template | Report theme |
|-------|-----------------|--------------|
| 1 | Qwen's `layout_template_id`, if the data can fill that template | An explicit prompt term (for example "colorblind", "finance") |
| 2 | A prompt term from `layout_templates.json` | Qwen's `report_theme_id` |
| 3 | `classic_overview` (the previous fixed layout) | `default` (Power BI base theme only) |

The choice and its source are recorded in `planning_provenance`
(`layout_template`, `layout_template_source`, `report_theme`,
`report_theme_source`), so they are covered by every draft hash.

Only bare IDs are offered to the model. The Light profile budgets prompt bytes
(4,096 minus output and overhead), and descriptive option text pushed ordinary
profiles past it. If a backend still rejects the prompt for its context bound,
the planner retries once with the narrative-only prompt and ignores any design
IDs in that answer.

The narrative call is deliberately lean because CPU prefill dominates it (about
30 tokens/s and 6.5 generated tokens/s for the 4B profile on the development
laptop):

- It sends only the request, the profile and the design IDs. Knowledge-base
  references and DAX/M worked examples stay with chat, DAX Lab and revisions,
  where they change the answer (`DatasetNarrativePlan.worked_examples = False`).
- The generation schema requires `layout_template_id` and `report_theme_id`
  (null allowed) and lists them first. Constrained decoding otherwise skips
  optional keys, so the model never chose a design.
- The summary is capped at about 40 words, and the call is bounded at 75 seconds.
- The model describes what the dashboard lets people examine. The profile has
  no results, so it must not state findings or causes.

Measured on the two-table sample (13,824 sales rows), the narrative went from
always timing out to completing in 44–51 seconds. The model chose `driver_analysis`
with `finance_neutral` for "Why did profit slip…", and `executive_scorecard` for
a CFO board view.

Charts follow the measure a request names: "Why did profit slip?" binds Profit
first in both template and classic layouts, without reordering the stored
measures.

## Templates

`src/ai_powerbi_analyst/studio/layout_templates.json` holds six structures derived
from dashboards in [Power BI Design Files](https://github.com/Dashboard-Design/Power-BI-Design-Files)
(MIT). Visual positions were read with `studio.report_import.read_report` and
snapped to Studio's 12x12 grid and the minimum sizes in `report_visuals.json`.

| ID | Derived from | Needs |
|----|--------------|-------|
| `ranked_performance` | Agents Performance | 2 measure/category pairs |
| `kpi_rail_trend` | Furniture Sales | date, 3 pairs |
| `driver_analysis` | Root Cause Analysis | date, 2 pairs |
| `comparison_grid` | Sales Teams Quarterly Performance | 4 pairs |
| `operations_monitor` | IT Support Performance | 2 pairs |
| `executive_scorecard` | Inventory Analysis; Awesome Chocolates | date, 3 pairs |

A slot names a recipe, a catalog visual type, a ranked measure/category index and
a grid rectangle. `layout_templates.bind_template` fills slots from the same
ranked fields the classic layout uses; each chart gets a distinct
measure/category pair. Missing KPI measures leave a gap. Bound pages go through
the normal `_validate_pages` and layout normalization. If validation fails, the
draft falls back to `classic_overview`.

Prompt terms are deliberately specific ("top 10", "root cause", "side by side").
Generic words such as "trend" keep the classic layout, which already has a
Trends & Drivers page.

## Themes

`src/ai_powerbi_analyst/powerbi/report_themes/registry.json` pins each theme by
SHA-256. `theme.register_studio_theme` writes it with the PBIR `customTheme`
contract during a Studio build; `default` writes nothing. Requirements:

- The theme JSON `name` must equal its file name (Microsoft's PBIR validator
  reports `PBIR_THEME_FILE_NAME_MISMATCH` otherwise).
- Text and the first five data colors must meet WCAG contrast against the card
  surface (4.5:1 text, 3:1 marks). `executive_light` is the frozen blueprint theme
  and is checked on four colors because its fifth, `#D6A019`, is 2.4:1.
- `visualStyles` may style native visuals only.
- Files are LF-only (`.gitattributes`) because line endings change the hash.

Local previews (designer, DAX Lab) draw marks with the draft theme's data colors,
from `GET /v1/report-themes`, so they match the build. The default theme uses Power
BI's base palette.

Review & build shows the chosen layout and its source. The theme preset picker and
the draft theme mutation route were removed on 2026-10-02. Draft generation and
builds retain their existing theme metadata, and the read-only palette route remains
available to the renderer. Studio does not offer theme changes for imported reports.

Explicit prompt terms outrank the model for both layouts and themes. For example,
"an overview, trends and detail page" selects `classic_overview` even if the
model picks another template.

## Changing a template or theme

1. Edit the JSON (and `LayoutTemplateId` / `ReportThemeId` for a new ID).
2. For a theme, update its `sha256` in `registry.json`.
3. Run `uv run pytest tests/unit/test_studio_layout_templates.py tests/unit/test_report_themes.py`.
4. Reseal the development runtime, because these files are pinned:
   `.venv\Scripts\python.exe scripts\reseal_dev_runtime.py --note "<reason>"`.
