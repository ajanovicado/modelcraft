# Imported report render contract

Status: 2026-09-29 (round 3: sections 12 to 22 are new, and sections 1, 2, 3, 7, 8 and 10 gained
keys; the round 3 fix pass added the request keys `highlight` and `drill_source`, the page keys
`notes` and `drillthroughs`, the row keys `dataNotes` and `declined`, `formatting.rangeDependent`,
and section 23, which lists every change the client must know about). This is the payload the canvas (`InteractiveReportPage.tsx` and the Visual pane) draws an
imported dashboard from. It is written for the front-end agent: code against this file alone. The
producer is `studio/live_report.py` (`render_page`, `render_formatting`,
`render_formatting_payload`, `data_key`) with `studio/report_formatting.py`, `report_tokens.py`,
`report_props.py`, `report_theme.py`, `render_rules.py` (conditional formatting, number formats),
`render_analytics.py` (analytics lines, visual calculations, small multiples) and
`render_navigation.py` (bookmarks, actions, pages, interactions, sync, phone layout) behind it.
Design context: `imported-report-editing.md` and the review items P6, P11, P17, P18, P19, P26 and
P34 in `imported-report-editing-review.md`.

Everything here is additive: every key the canvas reads today (`label`, `showLabel`, `showValue`,
`titleVisible`, `fontSize`, `references`, `categoryColors`, `categoryAxisTitle`, `valueAxisTitle`,
`grid`, `tableGrid`, `background`, `slicerStyle`, `imageIndex`, `image`, `rows`, `columns`, `error`)
is still sent with its old meaning, so the canvas can move to the typed tokens one visual kind at a
time.

Two rules for every value below:

- **Nothing is markup.** Every color, font family and text run was validated in Python. Render text
  with `textContent`, never `innerHTML`; put colors and font families into `style` as given. A `null`
  color means "no color: use your default".
- **Units.** Font sizes and lengths are CSS pixels (a Power BI point is 4/3 px; the server already
  converted). Positions are page pixels (the page is `page.width` x `page.height`). Transparency is
  folded into `#RRGGBBAA` colors or into an `alpha` between 0 and 1.

## 1. The page response

`render_page(engine, report, page_id, request, only=None, extra=None, revision=None)` returns:

| Key | Type | Meaning |
|---|---|---|
| `visuals` | `Visual[]` | Every visual of the page (or of `only`), **sorted by `rank` ascending** |
| `elapsed_ms` | int | Server time |
| `engine` | string | Who computed the rows ("Studio DAX engine", "Power BI Desktop") |
| `page_style` | `PageStyle` | The canvas behind the visuals |
| `theme` | `Theme` | The report's effective theme |
| `revision` | string or null | The `revision` the request asked for, echoed unchanged |
| `head_hash` | string | sha256 of the file the response was drawn from (the head revision's) |
| `page` | `{id, title, width, height, hidden, page_type, binding}` | `page_type` is `default`, `drillthrough` or `tooltip`; `binding` in section 18 |
| `bookmarks` | `Bookmark[]` | The report's bookmarks in pane order (section 17) |
| `bookmark` | object or null | The bookmark the request drew the page as, and what of it is not applied (section 17) |
| `interactions` | `{source: {target: how}}` | How each visual reacts to a selection in another (section 19) |
| `sync_groups` | `{group: [{page, visual, hidden}]}` | Synced slicers of the whole report (section 19) |
| `mobile` | `{visuals: {id: box}}` or null | The page's phone layout (section 20) |
| `drillthroughs` | `Drillthrough[]` | The report's drillthrough pages in page order (section 18) |
| `notes` | string[] | Why the page's data visuals are approximate, in plain English (section 18); `[]` when nothing is missing. Show them once for the page |

The request body (`LiveReportRequest`, the POST body of both render routes) is:

```json
{ "selections": [{ "visual_id": "slicer", "values": ["West"] }],
  "searches": { "slicer": "we" },
  "bookmark": "Channel focus",
  "drill": [{ "field": "DimStore.StoreName", "values": ["Store 1"] }],
  "drill_source": { "page": "ReportSection8454c812b60b8bb83b99", "visual": "b404a461ede7604a2774",
                    "selections": [{ "visual_id": "slicer", "values": ["West"] }] },
  "highlight": true }
```

`bookmark` (a name or display name, section 17), `drill` (one object or a list of up to 24,
section 18), `drill_source` (the page a drillthrough or tooltip was opened from, section 18) and
`highlight` (section 19, default false) are optional; an unknown bookmark, a drill on an ordinary
page, a drill field a page without "Keep all filters" does not take, or a `drill_source.visual`
that is not on its page is a 422 with a plain message.

`PageStyle`:

```json
{ "color": "#ffffff", "alpha": 1.0,
  "wallpaper": { "color": "#ffffff", "alpha": 1.0 },
  "image": "data:image/png;base64,...", "scaling": "Fit" }
```

- `color` and `alpha` are the page background (`alpha` = 1 - transparency/100). A page without a
  background object takes the theme's: the base theme says transparency 100, so `alpha` is 0 and the
  wallpaper shows. Paint the wallpaper (`wallpaper.color` at `wallpaper.alpha`) behind the page
  rectangle, then the page background over it, then the image.
- `image` and `scaling` are present only when the page has a background image (`scaling` is `Fit`,
  `Fill` or `Normal`; `Fill` is CSS `cover`, `Fit` is `contain`). `image` is a PNG, JPEG or GIF data
  URI checked by signature, at most 4 MB.

`Theme` (from `theme_payload`; the theme is the custom theme deep-merged over the base theme):

```json
{ "name": "Fixture theme",
  "dataColors": ["#0B6E4F", "#F26419", "#33658A"],
  "palette": ["#ffffff", "#000000", "#0b6e4f", "#f26419", "#33658a"],
  "foreground": "#252423", "background": "#FFFFFF", "tableAccent": "#118DFF",
  "foregroundNeutralSecondary": "#605E5C", "foregroundNeutralTertiary": "#B3B0AD",
  "backgroundLight": "#F3F2F1", "backgroundNeutral": "#C8C6C4",
  "textClasses": { "callout": { "fontSize": 45, "fontSizePx": 60, "fontFamily": "DIN", "color": "#252423" },
                   "title": {...}, "header": {...}, "label": {...} } }
```

`dataColors` is the series palette. `palette` is what a `ThemeDataColor` id indexes (section 4). A
chart with no explicit color draws `theme.dataColors[0]` (Desktop paints `#00A651` under the test
theme; the canvas used to paint a fixed `#477fbc`, which must go). `foreground` and `background`
are informational: they are **not** what ColorId 1 and 0 resolve to.

## 2. The visual row

```json
{
  "id": "b404a461ede7604a2774",
  "title": "Sales by Region",
  "kind": "column",
  "visual_type": "clusteredColumnChart",
  "container_kind": "visual",
  "position": { "x": 20, "y": 30, "width": 400, "height": 240 },
  "z": 1000, "rank": 3,
  "hidden": false, "hidden_own": false,
  "parent_group": null, "group_children": [],
  "fidelity": "approximate", "unrendered": ["categoryAxis.innerPadding"],
  "data_key": "9c1e...64 hex",
  "columns": [ ...Column ],
  "rows": [["West", 5.0]], "formatted_rows": [[null, "5"]],
  "selected": [], "truncated": false,
  "sort": [{ "index": 1, "descending": true }],
  "formatting": { ...Formatting }
}
```

| Key | Type | Notes |
|---|---|---|
| `id` | string | The visual's name. Unique on the page |
| `title` | string | The text Studio lists the visual by. For a chart with no title text it is the automatic title (section 5) |
| `kind` | string | `card column bar line area pie donut table matrix slicer textbox shape image group button navigator unsupported`. What to draw |
| `visual_type` | string | The PBIR type (`cardVisual`, `clusteredColumnChart`, `visualGroup`...) |
| `container_kind` | `visual` or `group` | A group has no content and no query |
| `position` | `{x, y, width, height}` | Page pixels. Members of a group use page coordinates too: the importer adds the group origin (Desktop stores members relative to their group, nested groups add up), so the client needs no group arithmetic |
| `z`, `rank` | int | Raw z and stacking rank 1..n (section 6) |
| `hidden` | bool | Effective: the visual's own `isHidden` **or** any ancestor group's |
| `hidden_own` | bool | The visual's own flag |
| `parent_group`, `group_children` | string or null, string[] | The group tree. `group_children` lists a group's direct members, bottom of the stack first |
| `fidelity` | `exact`, `approximate`, `none` | Section 7 |
| `unrendered` | string[] | Set properties Studio does not draw, as `object.property` |
| `data_key` | string | Identifies this row's data (section 9) |
| `columns` | `Column[]` | `[]` for content visuals, groups, unsupported visuals and errors |
| `rows` | `Value[][]` | At most 100 rows, in the report's sort order. A value is string, number, boolean or null |
| `formatted_rows` | `(string or null)[][]` | Number-formatted text where the model has a format string; `null` means "format the raw value yourself" |
| `selected` | `Value[]` | The slicer's or chart's selected values (saved selection unless the request overrides it) |
| `truncated` | bool | More than 100 rows existed |
| `sort` | `{index, descending}[]` | Present for visuals that have a query |
| `extra` | array | Only when the request asked for extra card figures |
| `formatting` | `Formatting` | Section 3. Never absent; `{}` only when the visual failed before formatting could be read |
| `error` | string | Section 8. Present when the visual cannot be drawn |
| `needs_desktop` | true | Present with `error` when Power BI Desktop could draw what Studio cannot |
| `unsupported_reason` | string | Present for visuals Studio only frames (section 8) |
| `action` | `Action` | What clicking the visual does, when it has an action (section 16) |
| `tooltipPage` | `{page, title, width, height}` | The report page shown as its tooltip (section 18) |
| `syncGroup` | `{groupName, fieldChanges, filterChanges}` | A synced slicer's group (section 19) |
| `totals` | `{values, formatted}` | Data. A table's totals row (section 14) |
| `highlights` | `Value[][]` | Data. Rows as a selection elsewhere highlights them (section 19) |
| `referenceLines` | `Line[]` | Data. Analytics lines with their values (section 15) |
| `smallMultiples` | `{index, field, values, panels}` | Data. The panels of a split chart (section 21) |
| `dynamic` | `{path: value}` | Data. Titles and backgrounds a measure gives (section 21) |
| `dataNotes` | string[] | Data. Why this visual's values are approximate for a reason only its data shows (section 23); absent when none |
| `declined` | true | Data. Present with `error` and `needs_desktop` when Studio's engine declined the visual because it reads something the local model lacks (section 8) |

`Column`: `{ kind: "column"|"measure"|"computed", table, name, label, role, query_ref, format,
alignment, colorIndex, hidden, backgroundIndex?, icon?, displayUnits?, decimals?, computed?,
dataBars? }`. Every column carries the ten keys before `backgroundIndex`, a helper or computed
column too, with these defaults: `kind "column"`, `table ""`, `name ""`, `label ""`, `role ""`,
`query_ref null`, `format ""`, `alignment ""`, `colorIndex null`, `hidden false`. The others are
present only when they apply.

- `label` is the display name; `format` is the number format string the values are formatted
  with: the visual's own format for the field when it sets one (section 13), else the model's (""
  when none).
- `alignment` is `""`, `left`, `center` or `right` (a table column's alignment).
- `dataBars` is `{ positive, negative, hideText, axis, reverse, min, max }` for a table data-bar
  column (colors validated; `axis` is the axis color or null; `reverse` draws the bars right to
  left; `min`/`max` are the bar's bounds or null for "from the data": the lowest and highest value
  of the column, with 0 as the axis when there are negative values; `hideText` is "show bar only").
- `colorIndex` is the index into `columns` of a hidden column whose value (in each row) is the font
  color of this column's cells (a conditional font color); it is `null` otherwise.
  `backgroundIndex` is the same for the cell background (tables). The row values at those indexes
  are validated colors or `null` (no format: your default). See section 12.
- `icon` is `{ index, layout, alignment }` for a column with conditional icons: the row value at
  `index` is an icon name (section 12), `layout` is `before`, `after` or `only` (icon only),
  `alignment` is `top`, `middle` or `bottom`.
- `displayUnits` and `decimals` are present when the visual sets them for the field; the
  `formatted_rows` already apply them (section 13).
- `kind: "computed"` is a column the rows give but the query does not (a conditional format's
  colors or icons, a visual calculation); `computed` says which: `gradient`, `rules`,
  `fieldValue`, or `visualCalculation`. Its `formatted_rows` cells are `null`.
- `hidden: true` marks a column not to draw: a helper the formatting reads (`role:
  "Formatting"`), a Tooltips-well field (`role: "Tooltips"`, shown on hover in Power BI), a
  projection the report hides (a visual calculation's input) or the column the model sorts a
  category by (`role: "SortBy"`: Month is ordered by MonthNumber). `sort[].index` may point at a
  `SortBy` column: the rows already arrive in that order, and an axis follows the row order.
- A hidden helper that only the whole visual needs (the measure of a dynamic title or background,
  of a measure-driven constant line) is `null` in every row: its value is in `dynamic` or
  `referenceLines`.

## 3. `formatting`: the typed tokens

Which tokens a row carries depends on its `kind`. Common to every non-group visual:
`title`, `container`, `automaticTitle`, and the legacy keys of the introduction.

| kind | Tokens |
|---|---|
| `column bar line area pie donut` | `chart`, `labels`, `legend`, `axes`, `series` (+ `line` for line and area, `smallMultiples` when split) |
| `card` (cardVisual and card) | `cardValue`, `cardLabel`, `tile` (cardVisual only, else `null`), `references`, `imageIndex`, `image` |
| `table` | `table` |
| `slicer` | `slicer`, `tile` (advancedSlicerVisual only), `label`, `slicerStyle` |
| `textbox` | `content` |
| `shape` | `shape` |
| `image` | `picture` |
| `button` | `button` (section 16) |
| `navigator` | `button`, `navigator` (section 16) |
| `group` | `container`, `titleVisible` only |
| `matrix`, `unsupported` | `title`, `container` (the frame) |

Any visual whose title or background a measure gives also has `dynamicProperties` (section 21).

Where a value is not set in the visual, the server fills it from the theme: the visual's own entry
by selector specificity (default, then metadata, then data, then state), then the theme's
`visualStyles[type]`, then `visualStyles["*"]`, then `textClasses` (title -> `title`, card value ->
`callout`, axis and data labels -> `label`, slicer and table headers -> `header`, table values ->
`label`), then a built-in default. The client never needs the theme to resolve a token; `theme` is
there for colors the renderer picks itself (default series colors).

### `title`

```json
{ "show": true, "text": "Total MTD Sales", "textSource": "literal", "automaticText": null,
  "fontSize": 16.0, "fontColor": "#1F2933",
  "fontFamily": "\"Segoe UI Semibold\", wf_segoe-ui_semibold, helvetica, arial, sans-serif",
  "bold": false, "italic": false, "underline": false, "alignment": "left", "wrap": true,
  "background": null }
```

- `show`: the title row is drawn. Defaults: on for charts (`column bar line area pie donut`), off for
  tables, matrices, cards, slicers, textboxes, shapes, images. An explicit `title.show` wins; then
  the theme's; text typed into a title that has no `show` counts as shown.
- `textSource`: `literal` (the user's text), `automatic` (Power BI's `<measures joined " and "> by
  <category> and <series>`, in `text` and `automaticText`), `dynamic` (a measure drives the title:
  `text` is `""`, draw nothing or a placeholder, and `fidelity` is `approximate`), `none`.
- `automaticText` is always sent when it can be computed, even when hidden or overridden, so the
  renderer can identify an automatic caption such as "Sales by Region".
- `alignment` is `left`, `center` or `right`. `background` is `#RRGGBB(AA)` or null.

### `container` (the visual's box)

```json
{ "background": "#ff880099",
  "border": { "show": true, "color": "#123456", "width": 2.0, "radius": 8.0 },
  "shadow": false, "padding": [5.0, null, null, 7.0] }
```

- `background`: `#RRGGBBAA` (color plus transparency), or `null` for no background. The default is
  the theme's `background` color at full opacity for every type except textbox, shape and image,
  which default to no background. That is white under the default theme and a dark color under a
  dark theme (`#203040ff` for a theme whose background is `#203040`): the container default follows
  the theme, not a hard-coded white. Desktop was only measured with themes whose background is
  white, so the dark-theme default is the design decision, not a measured fact; a visual that sets
  `background.color` is exact either way. **Always trust this token; do not make backgrounds
  transparent because the page has an image.**
- `border.show` false gives `color` and `width` null and `radius` 0. Draw `radius` only with the
  border on.
- `shadow`: a plain drop shadow of your choice when true.
- `padding`: `[top, right, bottom, left]` in px, each side a number or `null` (your default), or
  `null` when nothing is set.

### `chart`, `labels`, `legend`, `axes`, `series`, `line`

```json
{ "chart":  { "orientation": "vertical", "stacking": "clustered" },
  "labels": { "show": true, "fontSize": 16.0, "color": "#252423", "family": "\"Segoe UI\"",
              "bold": false, "italic": false, "displayUnits": 1000.0, "decimals": 1,
              "position": "OutsideEnd", "style": null },
  "legend": { "show": true, "position": "top", "alignment": "start", "fontSize": 13.33,
              "color": "#252423", "family": "\"Segoe UI\"" },
  "axes":   { "category": { "show": true, "title": false, "titleText": null, "titleColor": "#000000",
                            "titleFontSize": 13.33, "fontSize": 13.33, "color": "#000000",
                            "family": "\"Segoe UI\"", "gridlines": false, "gridlineColor": null },
              "value": { "...": "same keys" } },
  "series": { "defaultColor": "#1E90FF", "seriesColors": { "Orders.Sales": "#0B6E4F" },
              "categoryColors": [ { "value": "Others", "color": "#cccccc", "column": "Orders.Region" } ],
              "varyByCategory": false },
  "line":   { "strokeWidth": 3.0, "markers": false } }
```

- `chart.stacking`: `clustered`, `stacked` or `none` (lines, areas, pie). PBIR calls the stacked
  charts `columnChart` and `barChart` and the clustered ones `clusteredColumnChart` and
  `clusteredBarChart`; `orientation` is `horizontal` for bars.
- `labels.show` defaults to false. `displayUnits` is a divisor (1000, 1000000...) or null for Auto;
  `decimals` is null for Auto; `position` is one of `Auto OutsideEnd InsideEnd InsideCenter InsideBase`
  or null; `style` is a pie/donut label style text or null. `labels.format` is present when the
  labels have their own format string (Power BI's "Custom" display units): format label values
  with it instead of the column's `format`.
- `axes.*.displayUnits` and `axes.*.decimals` are the axis labels' display units (a divisor or
  null for Auto) and decimals (null for Auto), as for data labels.
- `legend.position` is `top bottom left right`, `alignment` `start` or `center`. The client shows a
  legend only when there is more than one series, and only when `show` is true (a theme can turn a
  chart type's legend off).
- `axes.*.gridlines` defaults: category false, value true. `title` is the "Axis title" switch;
  `titleText` overrides the default title (the field name).
- `series`:
  - A mark's color is, in order: a matching `categoryColors` entry (`value` equals the category,
    and `column` and `metadata`, when present, name the same column or measure), then
    `seriesColors[query_ref of the measure]`, then `defaultColor`, then `theme.dataColors[i]` where
    i is the series index (`dataColors[0]` for a single series).
  - `categoryColors[].value` is a string, number (a `2019L` or `19.5M` literal arrives as a number),
    boolean or, with `"type": "datetime"`, an ISO text `YYYY-MM-DDTHH:MM:SS`; compare a date
    category by parsing the row's date the same way. `column` is `Table.Column` of the scope field.
    `metadata` (when present) is the `query_ref` of the measure the color applies to.
  - `varyByCategory` is true for pie and donut and for a chart with "vary colors by category": each
    category takes the next `theme.dataColors` entry unless an entry colors it.
  - Colors already went through ThemeDataColor resolution (section 4).
  - `ruleColors` is `[{ metadata, index }]`: conditional data colors (section 12); `metadata` is
    the `query_ref` of the measure (series) a rule colors, or null for every series. A rule wins
    over every color above: a point's color is its row's value at `index` when that is not null,
    and only otherwise the order above.
- `line` only for `line`, `area`.

### `cardValue`, `cardLabel`, `tile`, `references`

```json
{ "cardValue": { "show": true, "fontSize": 45.33, "color": "#1F2933",
                 "family": "\"Segoe UI Bold\", wf_segoe-ui_bold, helvetica, arial, sans-serif",
                 "bold": false, "italic": false, "alignment": "left" },
  "cardLabel": { "show": true, "text": "Agents with Positive Change %", "fontSize": 13.33,
                 "color": "#52606D", "family": "\"Segoe UI\"", "bold": false, "italic": false,
                 "alignment": "left" },
  "tile": { "default":  { "fill": "#f1f2f280", "fillShown": true,
                          "outline": { "show": true, "color": "#e6e6e6", "width": 1.0, "transparency": 0.0 },
                          "padding": [null, null, 9.0, 13.0],
                          "accentBar": { "show": false, "color": null, "width": null, "position": null } },
            "selected": null } }
```

- `cardValue.fontSize` defaults to the theme's `callout` class (45 pt = 60 px in the base theme),
  clamped to 10 to 72 pt. A legacy `card` also gets `displayUnits` and `decimals` in `cardValue`.
- `cardValue.colorIndex` and `cardLabel.colorIndex` (null or a column index) give the value's and
  the caption's conditional font color: the first row's value at that index (section 12);
  `tile.default.fillIndex` does the same for a new card's tile fill.
- `cardLabel.show` false hides the caption; `text` is the label text (or the visual's caption).
- `tile.default` describes the tile's fill and outline. `fill` is `#RRGGBBAA` or null; `fillShown`
  false means the fill switch is off. `padding` is `[t, r, b, l]` (null sides: your default) or
  null. `tile.selected` is the same for the `selection:selected` state, or `null` when the visual
  does not style it. `tile` is `null` for a legacy `card`.
- `references` (unchanged): `[{ index, label, detailIndex }]`, indexes into `columns`; the values
  come from the hidden helper measures. `imageIndex` is the column of a measure-driven SVG; after
  the query, `formatting.image` is the sanitized SVG data URI (or null).

### `table`

```json
{ "table": {
    "header": { "fontSize": 16.0, "color": "#252423", "family": "\"Segoe UI Semibold\", ...",
                "background": null, "alignment": null, "wrap": true },
    "values": { "fontSize": 16.0, "color": "#000000", "family": "\"Segoe UI\"",
                "secondaryColor": "#000000", "banding": { "primary": null, "secondary": null },
                "wrap": false },
    "grid":   { "horizontal": false, "vertical": false, "horizontalColor": "#ffffff",
                "verticalColor": null, "horizontalWeight": null, "verticalWeight": null,
                "rowPadding": 9.0 },
    "columns": { "Orders.Rank": { "alignment": "center", "styleHeader": true, "width": 78.92,
                                  "fontColor": null, "background": null,
                                  "displayUnits": null, "decimals": null } },
    "total": { "show": false, "label": "Total", "fontSize": 16.0, "color": "#000000",
               "family": "\"Segoe UI\"", "background": null, "bold": true } } }
```

- Fonts come from `columnHeaders` (default: `header` class) and `values` (default: `label` class).
  **The font size is independent of the grid**: do not shrink the font when `grid.horizontal` is
  false.
- Row banding: odd rows use `banding.primary` as background and `values.color`, even rows use
  `banding.secondary` and `values.secondaryColor`; null means no background.
- `wrap` is word wrap for headers and cells (Power BI's default is on). `horizontalWeight` and
  `verticalWeight` are the grid line widths in px (null: 1).
- `columns` is keyed by `Column.query_ref`; `alignment` is `left center right` or null; with
  `styleHeader` the alignment applies to the header too; `width` is px or null; `fontColor` and
  `background` are the column's own plain colors (Desktop's "Specific column", or a plain color
  given to every cell of the column); a conditional color (`Column.colorIndex` and
  `backgroundIndex`) wins over them for the rows it colors. `displayUnits` and `decimals` are
  already applied to `formatted_rows` (section 13).
- `total` is the totals row (section 14): draw it under the rows when `show` is true and the
  visual row has `totals`.

### `slicer`, `tile` (tile slicer), `label`, `slicerStyle`

```json
{ "slicer": { "mode": "dropdown",
              "header": { "show": true, "text": "Country", "bold": false, "fontSize": 16.0,
                          "color": "#252423", "family": "\"Segoe UI Semibold\"" },
              "items": { "fontSize": 13.33, "color": "#252423", "family": "\"Segoe UI\"" },
              "values": { "default": { "show": true, "fontSize": 16.0, "color": "#BCBEC0", "family": "...",
                                       "bold": false, "italic": false, "alignment": null },
                          "selected": { "...": "same keys or null" } },
              "layout": { "rows": 2, "columns": 2, "cellPadding": 8.0, "shape": "rectangleRoundedByPixel", "radius": 20.0 } },
  "tile": { "default": {...}, "selected": {...} },
  "slicerStyle": "tiles" }
```

`values`, `layout` and `tile` exist only for a tile slicer (`advancedSlicerVisual`, `slicerStyle`
`tiles`). `mode` (`dropdown`, `basic` which is a list, or `between`, `before`, `after`...) is
`dropdown` by default; the canvas draws `dropdown` and `basic`, anything else is `approximate`.
A tile with `tile.selected` uses it, `values.selected` and `layout` while selected.

### `content` (textbox)

```json
{ "content": [ { "align": "center", "runs": [
    { "text": "Agents Performance", "fontFamily": "\"Segoe UI Semibold\"", "fontSize": 24.0,
      "color": null, "bold": true, "italic": false, "underline": false, "hasLink": false } ] } ] }
```

Paragraphs of runs. `text` is plain text (markup-shaped content and control characters removed);
hyperlinks are dropped, and `hasLink` says a run had one so you may underline it without a target.
`fontSize` and `color` are null when the run sets none: inherit. At most 60 paragraphs of 120 runs.

### `shape`

```json
{ "shape": { "shape": "oval", "tileShape": "oval", "fill": "#ff0000ff",
             "outline": { "show": true, "color": "#000000", "width": 3.0 },
             "radius": null, "rotation": 0.0 } }
```

`tileShape` is what the report says, as one of Desktop's own `tileShape` values (matched without
regard to case): `arrow arrowChevron arrowPentagon heart hexagon line octagon oval parallelogram
pentagon pill rectangle rectangleRounded rectangleRoundedByPixel tabCutCorner tabCutTopCorners
tabCutTopCornersByPixel tabRoundCorner tabRoundTopCorners speechbubbleRectangle trapezoid
triangleIsoc triangleRight`. It is `null` when the report names a shape Desktop does not have.
`shape` is what the canvas should draw: one of `rectangle rectangleRounded rectangleRoundedByPixel
oval pill line`. Every other shape arrives as `rectangle` and the visual is `approximate` with
`shape.tileShape` in the fidelity signal; a client that can draw more shapes reads `tileShape`.
`radius` is the `roundEdge` (or `rectangleRoundedCurve`) number as written: for `rectangleRounded`
and `rectangleRoundedByPixel` use it as the corner radius, ignore it for the other shapes.
`fill` is null when the fill is off and defaults to `theme.dataColors[0]`. Desktop honors shape
formatting only when it is written under a state selector (`{"id": "default"}`); the server already
ignores entries written without one, exactly as Desktop does.

### `picture` (image visual)

`{ "picture": { "src": "data:image/png;base64,...", "scaling": "fill" } }`. `src` is a PNG, JPEG,
GIF or sanitized SVG data URI up to 4 MB, from a report resource or from an embedded `data:image`
literal (the bytes are checked by signature, never by the type the URI claims). It is null when the
resource is missing, is not an image, is an SVG with anything active in it, or when the report gives
a web address (never fetched) or a measure. Whenever the picture is set and `src` is null the
visual is `approximate` and `general.imageUrl` is in `unrendered`: draw an empty frame, not a broken
image. `scaling` is `normal`, `fit` or `fill`.

### Groups

A group (`kind: "group"`) has no title row, no content and no query. `formatting.container.background`
is the group's own background (usually null): draw a rectangle for it behind the members only when it
is not null. Draw nothing else for a group. `hidden` cascades to `group_children`.

## 4. Colors: `ThemeDataColor`

Colors in the payload are resolved on the server, but the palette is measured Desktop behavior
(2.157, `imported-report-editing.md` "Measured decisions") and the renderer needs it:

- ColorId 0 is always `#FFFFFF` and 1 is always `#000000` (not the theme's background and
  foreground). 2 to 9 are `dataColors[0..7]`. 10 and up are invalid: Desktop paints the default
  fill, so the server sends `null` for that color and marks the visual `approximate`. `theme.palette`
  is exactly this list.
- Percent shading: `p < 0` gives `floor(c * (1 + p) + 0.5)` per channel, `p > 0` gives
  `floor(c + (255 - c) * p + 0.5)`. A picker that writes a theme color writes ColorId 0 to 9 only.
- Transparency in Power BI is a percentage (0 opaque, 100 clear); the payload converts it to alpha.

## 5. Automatic titles

A chart whose title is on and has no text shows `<measure names joined " and "> by <category and
series joined " and ">` (Desktop: "Total Sales by ChannelName"). The server sends it as `title.text`
with `textSource: "automatic"` and as the row's `title`. Tables, cards and slicers have their title
off by default; if the user turns one on without text they get the same rule (`Sales` for a card with
one measure).

## 6. Stacking, hidden and groups

- Sort `visuals` by `rank` (already done) and set `zIndex` to `rank` (1..n), never to the raw `z`.
  `rank` orders by `z`, then `tab_order`, then file order, over **all** visuals of the page, so a
  partial response (`only`, or `render_formatting`) still has the page's ranks. Give each visual its
  own stacking context (`isolation: isolate`). A Desktop save reorders the visual folders, so the
  order rows arrive in is irrelevant.
- `hidden: true` visuals are not drawn in view mode. In edit mode draw them as ghosts (dashed
  outline, low opacity). They still filter: a hidden slicer's saved selection applies.
- A group has no box of its own (see Groups above) and is never an opaque white "needs a renderer"
  box. Its members are positioned on the page, not inside it; moving a group moves its members
  (server side, when the edit ops run).

## 7. Fidelity

`fidelity` says how much of what the file sets the canvas draws:

- `exact`: every property the visual sets is drawn.
- `approximate`: something set is not drawn (listed in `unrendered`), or a value could only be
  approximated (a conditional format or visual calculation Studio cannot evaluate, a measure
  Studio could not add past the 12-field limit, a dynamic title of a visual with a measure filter,
  a totals row under a measure filter, a field parameter's saved choice, an invalid theme color
  id, a slicer mode the canvas does not draw, a percent of parent over two or more levels, a
  drillthrough or tooltip page without the filters of the page it was opened from), or its data
  says so (`dataNotes`, section 23).
- `none`: the visual is only framed (Power BI only types, matrix, errors). Show "Check in Power BI".

`unrendered` lists `object.property` pairs (`categoryAxis.innerPadding`, `plotArea.transparency`);
container properties use the container object's name (`spacing.verticalSpacing`). A property the
canvas draws is still listed when its entry is scoped to something the tokens do not resolve: the
tokens read the value that applies to the whole visual (no selector, or `{"id": "default"}`), so
`labels.show` written for one series (`metadata` selector), a table's `values.fontColor` written for
one column or one cell, or a `selection:selected` fill on a card are set in the file but not drawn.
The scopes that are resolved are: `dataPoint` (series, categories, wildcards), a new card's `value`,
`label` and `image` and its reference labels (by field), a new card's conditional colors (by field
or every instance), a table's `columnFormatting` and `columnWidth` (by column) and its cell formats
`values.fontColor`, `values.backColor` and `values.icon` (every cell of a column), a field's own
`general.formatString`, the analytics lines (one entry per line, by id or measure), a button's
and navigator's state entries, and a tile slicer's `selection:selected` state. An entry whose
selector is not an object is dropped by the server and its properties are listed as not drawn.
Interaction-only objects (`visualTooltip`, `visualHeader`, `visualLink`, `general.altText`,
`general.filter`) are never listed: they do not change how a static frame looks (Studio follows
`visualLink` and `visualTooltip` in view mode: sections 16 and 18).

For retained Visual pane controls, ask "is this control drawn?" from the table. In Python it is
`report_formatting.property_is_rendered(visual_type, object, property, container=None)` and the table
is `report_formatting.RENDERED` (families `chart card cardVisual table slicer advancedSlicer textbox
shape image button navigator group unsupported`, plus `container` for the box objects `title
background border dropShadow padding` that every visual has). An edit whose property is not rendered should say
"visible in Power BI only" instead of "applied". The table answers for the whole visual; a control
that edits one series or one column (a `metadata` selector) is drawn only for the scopes listed
above, and `unrendered` of the visual is the authority after the edit.

## 8. Errors, `needs_desktop` and unsupported visuals

Per visual (the page still renders; the visual keeps its box, position, `title` and container
tokens):

```json
{ "id": "chart", "kind": "column", "error": "This page filter needs Power BI Desktop",
  "needs_desktop": true, "fidelity": "none", "columns": [], "rows": [] }
```

- `error` is plain English. `needs_desktop: true` means Power BI Desktop could draw it; offer
  "Preview in Power BI". Causes: a page or report filter Studio cannot compile ("This page filter
  needs Power BI Desktop": Top N, relative date, cross-table), a visual filter, a field missing from
  the model (a calculated column the imported data lacks), a custom visual interaction, a sort by a
  hidden field or by a visual calculation, and a field of a calculation group ("This visual uses
  the calculation group 'Time Calc', which Power BI applies") or of a field parameter ("... the
  field parameter 'Metric', which Power BI switches"), known from the model's facts. While the
  facts are not read yet, a table that may be one is treated as one ("Studio has not read this
  report's model, and 'Time Calc' may be a calculation group, which Power BI applies"): the
  tables the imported rows' model marks, or, without it, a table with an `Ordinal` column or with
  `X`, `X Fields` and `X Order` columns. A slicer on a calculation group with a selection (saved or
  in the request) changes every measure it reaches: each data visual with a measure that the
  slicer reaches (its interaction is not `none`) gets "The slicer on the calculation group 'Time
  Calc' changes the measures of this page, which Power BI applies". A field parameter's slicer
  changes no other visual. Row-level security is not a cause: Desktop's own view applies no role
  either.
- **Declined by Studio's engine** (`declined: true`, a data key): when the page is computed by
  Studio's own DAX engine, a visual whose query reads something the local model lacks (a column
  added after the data was read, a measure Power BI reports an error in) is left out of the
  query and gets that reason as `error` with `needs_desktop: true`; the rest of the page renders.
  A formatting-only render cannot know this (it runs no engine): when merging, keep `error`,
  `needs_desktop`, `declined` and `fidelity: "none"` of a cached row that has `declined` and the
  same `data_key` (section 10).
- **`unsupported_reason`** marks a visual Studio only frames: gauge, KPI, treemap, scatter, waterfall,
  funnel, maps, R and Python visuals, 100% stacked and combo charts, custom visuals, and matrices
  ("Matrix subtotals and hierarchy expansion are previewed in Power BI only"). Buttons and
  navigators are drawn since round 3 (section 16).
  The text is stable: `<Name> visuals are previewed in Power BI only`. `kind` is `unsupported`
  (`matrix` for matrices), `fidelity` is `none`, there is no `needs_desktop`. Draw the frame, the
  title and the reason; do not style it as a failure. Older clients read `error`, which repeats the
  reason; a client that reads `unsupported_reason` ignores `error` when both are present.
- A visual definition Studio cannot read at all (a projection or a value of the wrong type) gets
  the generic `This visual definition needs additional local support` with `needs_desktop: true`;
  it never fails the page.
- Content that needs no query (textbox, shape, image, group) is never affected by data filters.
- `render_page` and `render_formatting` give a visual the same `error`, `needs_desktop` and
  `fidelity`: the filters, interactions and sort that can fail are checked in both, without a
  query. A formatting row therefore never turns a `none` visual back into `exact`.

Whole page: `render_page` and `render_formatting` raise `ValueError` (HTTP 422 with the message) for
a bad request or a failed integrity check, and `live_report.Unsupported` (a `ValueError` with
`needs_desktop = True`) when a slicer's saved selection cannot be compiled, because every visual
would be drawn unfiltered. `compile_page` also raises `Unsupported` for a visual it is asked to
compute extra figures for (`extra=`) when that visual needs Desktop, so a caller never reads an
unfiltered number as the visual's own. The service is expected to treat `Unsupported` like an
engine decline: render the page in Power BI Desktop, or show the "needs Power BI Desktop" state.

## 9. Revisions and stale responses (P6)

Each edit moves the report's head revision. The head is `{ revision_id, hash }` in the imported
report payload; the client sends the id as `?revision=<id>` on the render calls, the server echoes
it as `revision` (and the file hash as `head_hash`) and returns 409 for a revision that is not the
head (service work). The client must:

1. Keep the current head id in a ref and put it in the effect dependencies and in the in-flight
   de-duplication key (path + body + head), so a new head starts a new request.
2. Drop every response whose `revision` is not the current head id, without clearing the loading
   state of the request that is current; do not merge a stale response into the cache.
3. After Move, Resize, Delete and Format-pane edits, update the layout optimistically, wait for the
   new head, then merge (section 10). An Undo is a new head like any other.

## 10. Formatting-only render (P19)

`render_formatting_payload(report, page_id, request, only, revision)` runs no DAX. It returns
`{ visuals: { <visual_id>: FormattingRow }, page_style, theme, revision, head_hash, page,
bookmarks, bookmark, interactions, sync_groups, mobile, drillthroughs, notes, elapsed_ms }`.
A `FormattingRow` is a visual row without its data: it has every key of section 2 except `id`
and the data keys `rows`, `formatted_rows`, `selected`, `truncated`, `sort`, `extra`, `totals`,
`highlights`, `referenceLines`, `smallMultiples`, `dynamic`, `dataNotes` and `declined` (the
server constant `live_report.DATA_KEYS`), so it carries `title`, `kind`, `visual_type`,
`position`, `z`, `rank`, `hidden`, `hidden_own`, `parent_group`, `group_children`, `fidelity`,
`unrendered`, `data_key`, `columns`, `formatting` and, when set, `error`, `needs_desktop`,
`unsupported_reason`, `action`, `tooltipPage`, `syncGroup`. **Keep every data key of the cached
row when merging**, and two consequences of the data keys:

- a cached row with `declined: true` keeps its `error`, `needs_desktop` and `fidelity: "none"`
  (the formatting row cannot know the engine declined it);
- a cached row with a non-empty `dataNotes` keeps `fidelity: "approximate"` when the formatting
  row says `exact`: `fidelity = worst(row.fidelity, cached.dataNotes ? "approximate" : "exact")`
  (`none` > `approximate` > `exact`). The formatting row's `unrendered` still replaces the cached
  one; the paths a failed piece adds (`total.totals`, a dynamic path, `<line object>.value`,
  section 23) come back with the next data render.

`data_key` is a sha256 of everything that decides a visual's rows and nothing else: its queryState
and sort, its own filters, page and report filters, the interactions aimed at it (and the report's
"cross-filter by default" setting), the measures its formatting adds to the query (rule-driven
colors, the image of a new card, reference labels and their details, the measure of a dynamic
title or background, and only while the switch that shows them is on), everything the rows are
computed with after the query (every conditional format rule, the analytics lines, a table's
totals switch, the visual's own format strings, display units and decimals, and the theme's
palette when a rule uses a theme color: the rows hold the resolved color, so a theme edit moves
the key of those visuals only), the slicers' saved selections, the user's selections, searches,
bookmark, drill values, `drill_source` and `highlight`, the tables Power BI draws (calculation
groups and field parameters, which change when the model facts are read) and the data model
(`model_key`: the DataModel's CRC and size). Titles, plain colors, fonts, positions and z order do
not change it. So a change to a conditional format's colors, an analytics line's style or a
column's display units moves the key and the client refetches the visual's data: its colors,
values and texts are computed in the rows. The same value is on every row of `render_page`.
Because a switch that adds a helper measure or a computed column changes both the key and the
`columns`, `columns.length` cannot differ while the key is equal; the length check below is only a
guard.

Client algorithm, after a Format-pane or layout edit that produced head `H`:

```
fmt = POST formatting-render(revision = H)            # the whole page: see the note on `only` below
if fmt.revision != currentHead: drop it
for each id, row in fmt.visuals:
    cached = cache[id]
    if cached and cached.data_key == row.data_key and cached.columns.length == row.columns.length:
        cache[id] = { ...cached, ...row }        # replace formatting, columns, position, rank, hidden, fidelity...
    else:
        refetch id with render_page(only = {id})  # the data itself changed (a field or filter edit)
replace page_style and theme with the response's
```

**The client does not send `only`.** The route runs no DAX, so asking for the whole page costs the same as asking for one
visual, and a partial answer would leave every visual it did not name with a stale `rank`, `position` and `hidden` (bring one
visual to the front and its neighbours' ranks change too; a group move or hide reaches its members). `only` stays accepted for
other callers. Two keys of a row come from the query and not from the formatting file, so a formatting-only answer lacks
them: `formatting.image` of a card (the picture a measure returns) is kept from the cached row while the row's `imageIndex` is
unchanged and the picture switch is still on, and is dropped when either changed.

`columns` must be replaced too: alignment, data bars, number formats and `colorIndex` are formatting
even though they live on the columns. The lengths match whenever `data_key` matches, because the
helper measures the formatting adds are part of the key (a test toggles the picture switch of a new
card and checks that the key and the columns move together).

A formatting-only render still reads the head file and the model's number formats. The formats are
cached by data model, and the first `render_page` of a report warms them, so the extra cost (about
a second, one subprocess) is paid only by a formatting render that is the first call after the
sidecar restarted. It is left as is on purpose: an empty format map would blank the `format` of
every column the client merges.

## 11. Examples

A clustered column chart (abbreviated; every key of section 3 is present, the rest are omitted here):

```json
{ "id": "chart", "title": "Sales by Region", "kind": "column", "visual_type": "clusteredColumnChart",
  "container_kind": "visual", "position": { "x": 20, "y": 30, "width": 400, "height": 240 },
  "z": 1000, "rank": 3, "hidden": false, "hidden_own": false, "parent_group": null, "group_children": [],
  "fidelity": "approximate", "unrendered": ["categoryAxis.innerPadding"], "data_key": "3c6936ab...",
  "columns": [ { "kind": "column", "table": "Orders", "name": "Region", "label": "Region", "role": "Category",
                 "query_ref": "Orders.Region", "format": "", "alignment": "", "colorIndex": null },
               { "kind": "measure", "table": "Orders", "name": "Sales", "label": "Sales", "role": "Y",
                 "query_ref": "Orders.Sales", "format": "", "alignment": "", "colorIndex": null } ],
  "rows": [["West", 5.0]], "formatted_rows": [[null, "5"]], "selected": [], "truncated": false,
  "sort": [{ "index": 0, "descending": false }],
  "formatting": { "title": { "show": true, "text": "Sales by Region", "textSource": "automatic", "...": "..." },
                  "container": { "background": "#ffffffff", "border": { "show": false, "color": null, "width": null, "radius": 0.0 },
                                 "shadow": false, "padding": null },
                  "chart": { "orientation": "vertical", "stacking": "clustered" },
                  "series": { "defaultColor": "#1E90FF", "seriesColors": {},
                              "categoryColors": [ { "value": "Others", "color": "#cccccc", "column": "Orders.Region" } ],
                              "varyByCategory": false },
                  "labels": { "show": true, "fontSize": 16.0, "...": "..." }, "legend": {}, "axes": {} } }
```

A hidden group and its member:

```json
{ "id": "grp", "kind": "group", "container_kind": "group", "title": "grp", "z": 500, "rank": 1,
  "hidden": true, "hidden_own": true, "parent_group": null, "group_children": ["kid"],
  "fidelity": "exact", "unrendered": [], "columns": [], "rows": [],
  "formatting": { "container": { "background": null, "border": { "show": false, "color": null, "width": null, "radius": 0.0 },
                                 "shadow": false, "padding": null }, "titleVisible": false } }
{ "id": "kid", "kind": "textbox", "z": 600, "rank": 2, "hidden": true, "hidden_own": false,
  "parent_group": "grp", "formatting": { "content": [ { "align": "left", "runs": [ { "text": "..." } ] } ], "...": "..." } }
```

A Power BI only visual:

```json
{ "id": "gauge", "kind": "unsupported", "visual_type": "gauge", "title": "gauge",
  "position": { "x": 660, "y": 30, "width": 200, "height": 120 }, "z": 4000, "rank": 6,
  "fidelity": "none", "unrendered": ["axis.min"],
  "unsupported_reason": "Gauge visuals are previewed in Power BI only",
  "error": "Gauge visuals are previewed in Power BI only",
  "columns": [], "rows": [], "formatting": { "title": {...}, "container": {...} } }
```

A visual with a filter Studio cannot compile:

```json
{ "id": "chart", "kind": "column", "error": "This page filter needs Power BI Desktop",
  "needs_desktop": true, "fidelity": "none", "columns": [], "rows": [],
  "position": { "x": 0, "y": 0, "width": 300, "height": 200 }, "formatting": { "title": {...}, "container": {...} } }
```

`tests/unit/test_live_report_editing.py` and `tests/unit/test_report_formatting_tokens.py` hold
exact-value examples of every token, including tiles per state, textboxes, shapes and images; they
are the executable form of this document. `tests/unit/test_render_round3.py` is the executable form
of sections 12 to 22.

## 12. Conditional formatting (computed per row)

Power BI's conditional formats (field value, gradient, rules, icons) are evaluated **on the
server, per row, after the query**. The canvas never evaluates a rule: it reads a color or an icon
name from a column of the row.

- **Field value** (a measure returns the color): the measure is a hidden helper column
  (`role: "Formatting"`); its values are sanitized to CSS-safe colors (or null).
- **Gradient** (2 or 3 colors; min/mid/max from the data or a number; blanks as zero, a specific
  color or no color) and **rules** (the first case that holds wins; bounds are numbers, text,
  "is blank", "any value" or a percent of the visual's range) become a **computed column**
  (`kind: "computed"`, `computed: "gradient" | "rules"`, `hidden: true`). Its values are `#rrggbb`
  or null. A gradient interpolates each RGB channel linearly, rounding half up; the lowest and
  highest values are those of the non-blank values of the rows drawn (at most 100; a truncated
  visual is approximate, section 23), and when they are equal every value takes the first stop's
  color. A blank is colored as 0 (clamped to the scale) when blanks count as zero, else with the
  blank color or none; a blank never widens the range.
- **Icons**: a computed column (`computed: "rules"`) whose values are Power BI's icon names
  (`ColoredArrowUp`, `CircleRedPatternFill`, `SignalBarFull`, `StarHighLight`, ... the names of
  Desktop's icon sets: `report_edit_conditional.ICON_SETS` lists them), or null for no icon. Map
  each name to a glyph; an unknown name draws nothing.
- The input a rule is based on (a measure or a summarized column the visual does not show) is
  added to the query as a hidden helper. The query holds at most 12 fields; a rule whose input no
  longer fits is not drawn and the visual is `approximate`, never an error.

Where the index lives, per target:

| Target | Where |
|---|---|
| Table cell font color | `Column.colorIndex` |
| Table cell background | `Column.backgroundIndex` |
| Table cell icon | `Column.icon.index` (+ `layout`, `alignment`) |
| Chart data colors | `formatting.series.ruleColors[].index` (by series `metadata`, or every series) |
| New card value / caption font color | `formatting.cardValue.colorIndex`, `formatting.cardLabel.colorIndex` |
| New card tile fill | `formatting.tile.default.fillIndex` |
| Legacy card value color | `formatting.cardValue.colorIndex` |

Example: a table whose Sales cells have a black-to-white background scale.

```json
{ "id": "t", "kind": "table",
  "columns": [
    { "kind": "column", "name": "Region", "query_ref": "Orders.Region", "colorIndex": null, "backgroundIndex": null, "...": "..." },
    { "kind": "measure", "name": "Sales", "query_ref": "Orders.Sales", "colorIndex": null, "backgroundIndex": 2, "...": "..." },
    { "kind": "computed", "name": "values.backColor", "label": "values.backColor", "role": "Formatting",
      "hidden": true, "computed": "gradient", "table": "", "format": "" } ],
  "rows": [["A", 0.0, "#000000"], ["B", 5.0, "#808080"], ["C", 10.0, "#ffffff"]],
  "formatted_rows": [[null, null, null], [null, null, null], [null, null, null]] }
```

Data bars (section 2, `Column.dataBars`) are drawn by the canvas from the column's own values.

## 13. Number formats

The server formats `formatted_rows` with the visual's own settings when it has them, in this
order: the projection's own format string (Desktop's visual-level format), else the field's
`general.formatString`, else the model's format; then a table column's display units and decimals
(`columnFormatting`), a new card's value units, decimals and custom format, or a legacy card's
`labels` units and decimals. Power BI's suffixes are `K`, `M`, `bn` and `T`; display units do not
scale a percentage. A card whose units are Auto picks `K`, `M` or `bn` by size, as before.
Decimals and units rewrite every `;` section of the format string (positive, negative, zero), and
the unit suffix is quoted inside each section, so `\$#,0;(\$#,0)` in thousands with no decimals
formats -1500 as `($2K)` (a `%` inside quotes or after a backslash is text, not a percentage).

```json
{ "columns": [ { "name": "Sales", "format": "#,0", "displayUnits": 1000.0, "decimals": 1, "...": "..." } ],
  "rows": [["West", 15420.0]], "formatted_rows": [[null, "15.4K"]] }
```

Chart data labels and axes are formatted by the canvas: `labels.displayUnits`, `labels.decimals`,
`labels.format` (a custom format string) and `axes.value.displayUnits`, `axes.value.decimals`
(section 3).

## 14. Table totals

A table shows a totals row unless its `total.totals` switch is off (the Agents table's is off).
The totals are computed by a totals piece (the table's measures under its filters, with no
grouping) that runs in a second query after the page query, together with the highlight pieces
(section 19) and the whole-visual values of dynamic titles, measure lines and grand-total visual
calculations. If that query fails, each visual's pieces are asked on their own; a visual whose
pieces still fail keeps its rows, loses only its totals, highlights and whole-visual values
(listed in `unrendered`) and is approximate with a `dataNotes` entry (section 23). A page whose
page query rendered before therefore still renders. The totals are sent as data:

```json
{ "id": "t", "totals": { "values": [null, 15.0, 3.0], "formatted": [null, "15", null] },
  "formatting": { "table": { "total": { "show": true, "label": "Total", "fontSize": 13.33,
                                          "color": "#252423", "family": "\"Segoe UI\"",
                                          "background": null, "bold": true } } } }
```

`values` and `formatted` align with `columns`: category columns are null (draw `total.label` in
the first column), measures hold their total. A table filtered by a measure (a visual filter on a
measure, such as the Agents table's rank filter) gets no `totals` (a total without the filter
would be wrong) and is `approximate`. Formatting that reaches the totals row (a conditional
background, font color or icon whose selector's `matchingOption` is 0, values and totals, or 2,
totals only; data bars with `totalMatchingOption` 0 or 2) is listed in `unrendered`: Studio
formats no totals row. With 0 the value cells are still formatted; with 2 nothing is.

## 15. Analytics lines

A chart's analytics lines arrive with their values as data:

```json
{ "referenceLines": [
    { "id": "0", "object": "y1AxisReferenceLine", "kind": "constant", "axis": "value",
      "series": "Orders.Sales", "value": 7.0, "name": "Target", "color": "#FF0000", "alpha": 0.5,
      "style": "dotted", "width": null, "position": "front",
      "label": { "show": true, "text": "Value", "color": null, "decimals": null,
                 "displayUnits": null, "horizontal": "left", "vertical": "above" } },
    { "id": "1", "object": "referenceLine", "kind": "average", "axis": "value", "series": "Orders.Sales",
      "value": 5.0, "...": "..." },
    { "id": null, "object": "trend", "kind": "trend", "value": null, "start": 1.5, "end": 8.5, "...": "..." } ] }
```

- `kind`: `constant` (the value in the file), `measure` (a constant a measure gives for the whole
  visual, read from the whole-visual piece of the second query), `min`, `max`, `average`,
  `median`, `percentile` (computed over the series' values in the rows; PERCENTILE.INC, or
  PERCENTILE.EXC when the file says `Exclusive`, whose `value` is null below the first or above
  the last rank), `trend` (least squares over the points, x being the category's position in
  display order: draw from the first category at `start` to the last at `end`).
- A trend line with "Combine series" on (the default) is one line over every measure's points
  (`series` null). With it off there is one `trend` entry per measure, each with that measure's
  `query_ref` as `series`; draw each in its series' color. Off with a legend well (one line per
  legend series) is Power BI's to draw: `trend.combineSeries` is in `unrendered`.
- `axis` is `value` (a horizontal line on a column chart) or `category` (an X-axis constant line,
  whose `value` is a category value or an ISO date text).
- `style` is `solid`, `dashed` or `dotted` (`custom` draws dashed); `alpha` is the opacity;
  `position` `back` draws the line behind the marks; `width` null is 1 px.
- `label.text` is `Value`, `Name` or `ValueAndName`; format the value with `label.decimals` and
  `label.displayUnits`.
- `value` is null when there is nothing to compute from (no numbers). Error bars, forecasts,
  shading and anomaly detection are not drawn (`unrendered`).

## 16. Buttons, navigators and actions

`kind: "button"` (`actionButton`) and `kind: "navigator"` (`pageNavigator`, `bookmarkNavigator`)
are drawn from `formatting.button`:

```json
{ "button": { "shape": "rectangle", "tileShape": "rectangle", "radius": null,
    "states": {
      "default": { "fill": "#112233ff",
                   "outline": { "show": true, "color": "#605E5C", "width": 1.0 },
                   "text": { "show": true, "text": "Go", "fontSize": 16.0, "color": "#252423",
                             "family": "\"Segoe UI\"", "bold": false, "italic": false,
                             "underline": false, "alignment": "center", "verticalAlignment": "middle" },
                   "icon": { "show": true, "name": "rightArrow", "color": "#252423", "width": null,
                             "size": null, "placement": null, "alignment": null, "verticalAlignment": null } },
      "hover": { "fill": "#445566ff", "...": "same keys" },
      "selected": null, "disabled": null } } }
```

- A button's states are `default`, `hover`, `selected` (Desktop's "On press") and `disabled`; a
  navigator's are `default`, `hover`, `press` and `selected` (its current page or bookmark). A
  state the report does not style is null: use `default`. A styled state is complete: whatever it
  does not set the server filled from `default`, so draw it as given, never merge it yourself. A
  navigator has no `icon`.
- Which state to draw, first match wins: a button: `disabled` when disabled (below), `selected`
  while the pointer is pressed, `hover` under the pointer, else `default`. A navigator's item:
  `press` while pressed, `hover` under the pointer, `selected` for the item whose `selected` is
  true, else `default`.
- A button is disabled when its `action.valid` is false (the page, bookmark or address it points
  at does not exist), and a `drillthrough` button is disabled until a data point is selected on
  the page (a selection in a visual that has the target's bound field: see `drillthroughs`,
  section 18). A button with no `action` is not disabled. Desktop has no disabled state for a
  navigator.
- `icon.name` is Desktop's icon name (`blank`, `leftArrow`, `rightArrow`, `back`, `reset`, `help`,
  `information`, `qna`, `bookmarks`, ...): map it to a glyph.
- A navigator also has `button.layout` `{ orientation: "horizontal" | "vertical" | "grid", rows,
  columns, cellPadding }` and `formatting.navigator`:

```json
{ "navigator": { "kind": "pages",
    "items": [ { "label": "One", "action": { "type": "page", "target": "one", "valid": true }, "selected": true },
               { "label": "Four", "action": { "type": "page", "target": "four", "valid": true }, "selected": false } ] } }
```

  `navigator.kind` is `pages` (a page navigator) or `bookmarks` (a bookmark navigator, whose
  items have `action.type: "bookmark"`). A page navigator lists the pages Desktop shows (hidden
  and tooltip pages only when the navigator shows them); a bookmark navigator lists the bookmarks
  (of its group) in pane order.

Any visual with an action (a button, or a shape or image with one) has `action`:

```json
{ "action": { "type": "page", "target": "ReportSection3f1c...", "valid": true, "tooltip": "Next page" } }
```

`type` is `page` (go to `target`), `bookmark` (apply bookmark `target`, section 17), `back` (the
previous page), `drillthrough` (open page `target` with the drill values of the selected data
point, section 18), `url` (with `url`, an http(s) address or null; never open it without the
user's confirmation), `qna` (Power BI only) or `unknown`. `valid` is false when the target no
longer exists: draw the button disabled. For `qna` and `unknown` (an action type Studio does not
know) draw the button enabled, and on click do nothing but say "This button's action works in
Power BI only"; `target` is null for them.

## 17. Bookmarks

`bookmarks` in the page response lists the report's bookmarks:

```json
{ "bookmarks": [
    { "name": "Bookmarkc7a5beae9c2a6a44bb3c", "displayName": "Channel focus", "group": null,
      "page": "ReportSection8454c812b60b8bb83b99",
      "targets": ["54be5cf0783e66471540", "b404a461ede7604a2774"],
      "captures": { "data": true, "display": true, "page": true },
      "hidden": [], "shown": ["54be5cf0783e66471540", "b404a461ede7604a2774"] } ] }
```

`group` is `{name, displayName}` for a bookmark in a group; `targets` is null for a bookmark of the
whole page; `captures` says what applying it changes (filters and slicers, what is shown, the
page); `hidden`/`shown` are the visuals and groups it hides and shows on its `page`.

To apply one in view mode: navigate to its `page` when `captures.page` is true, then render that
page with `"bookmark": "<name>"` in the request. The server draws the page as the bookmark shows
it: `hidden` of each visual (a whole-page bookmark also shows what it does not list), each
slicer's selection (a whole-page bookmark clears the slicers it did not capture) and the report,
page and visual filter states it saved: those Desktop's capture keeps by name (`byName`: filters
hidden or locked in view mode), by field (`byExpr`) and by type (`byType`: include and exclude
filters), each applied to the report's filter of the same name, or on the same field when the
state has no name. A state Studio cannot apply (no name and no single filter on its field, or a
shape it cannot read) adds the note "Some filters this bookmark saved cannot be applied in Studio,
so the values can differ from Power BI's" and leaves the data visuals it reaches `approximate`
(all of the page's for a page or report filter). The response echoes it:

```json
{ "bookmark": { "name": "focus", "displayName": "Focus", "page": "one",
                "captures": { "data": true, "display": true, "page": true },
                "notes": ["The sort, drill level or field choices this bookmark saved are shown in Power BI only"] } }
```

`notes` (plain English, show them) name what is not applied: a spotlight, focus mode or layer
order it saved ("The spotlight this bookmark saved is shown in Power BI only", one note per
mode), a sort, drill level or field choice it saved **that differs from what the visual has now**
(a bookmark that saved the current sort needs no note), filters it saved that the report no
longer has, and "This bookmark opens another page" when it belongs to another page. The slicer and filter states a bookmark applies
are part of the data key, so rows it changes are cached apart; a bookmark that only shows and
hides visuals keeps the keys.

## 18. Drillthrough and tooltip pages

`page.binding` of a drillthrough or tooltip page is `{ type: "drillthrough" | "tooltip", fields:
[{ field: "DimStore.StoreName", table, column }], keepAllFilters, crossReport }` (null for an
ordinary page). `keepAllFilters` is Desktop's "Keep all filters" (on unless the file turns it
off); `crossReport` is a drillthrough other reports may target.

The page response's `drillthroughs` lists every drillthrough page of the report, in page order,
for a "Drill through" menu:

```json
{ "drillthroughs": [ { "page": "ReportSectiond2a4...", "title": "Store details",
                       "fields": [{ "field": "DimStore.StoreName", "table": "DimStore", "column": "StoreName" }],
                       "keepAllFilters": true, "crossReport": false } ] }
```

Offer a page for a data point whose row has a column whose `query_ref` is one of its `fields`.

- **Drillthrough**: render the page with `"drill": [{ "field": "DimStore.StoreName", "values":
  ["Store 1"] }]`: the values filter every visual of the page and replace the filter the page
  saved for that field (`pageBinding.parameters[].boundFilter`). With "Keep all filters" off only
  the page's bound fields are accepted. With it on (the default) Power BI also brings the source
  page's filters, slicers and selections, and any column is accepted: send them as
  `drill_source` (below). Without `drill_source` the page's data visuals are `approximate` and
  the response's `notes` says "The filters of the page this was opened from are not applied in
  Studio, so the values can differ from Power BI's".
- **Report-page tooltips**: a visual whose tooltip is a report page has `tooltipPage: { page,
  title, width, height }`. On hover, render that page with `drill` set to the hovered point's
  category (and series) values, e.g. `[{ "field": "Orders.Region", "values": ["West"] }]` (the
  `query_ref` of the category column), and draw it at `width` x `height` next to the pointer. A
  tooltip page accepts any column of the model and, like "Keep all filters", takes the source
  page's context from `drill_source` (else the same note). Cache the tooltip render by page,
  values and source.

`drill_source` is `{ page, visual?, selections? }`: the page the drill was opened from, the visual
whose point was drilled or hovered, and the selections active on that page (the same objects as
the request's `selections`; the drilled point itself belongs in `drill`, not here). The server
applies that page's page and report filters, its slicers (saved, or as `selections` sets them),
the visual's own filters, and the other selections that reach the visual (its interactions). A
filter on a measure only filters its own visual and cannot move to another page: it is left out
and the note is added. A `visual` that is not on `page` is a 422.

## 19. Interactions, highlights and synced slicers

`interactions` maps each visual whose selection can reach others (slicers, charts, tables) to how
every data visual of the page reacts: `filter`, `highlight`, `none` or `unsupported` (a type
Studio does not know: a selection in that source makes the target "needs Power BI Desktop"). It
follows the page's "Edit interactions" and Desktop's defaults: a slicer filters; a chart or table
highlights a column, bar, pie or donut chart and filters everything else; the report setting
"cross-filter by default" makes every default `filter`.

A selection is sent as before (`selections`). **Highlighting is opt-in**: only a request with
`"highlight": true` gets it. Without the flag every target whose interaction is `highlight` is
filtered instead, and the chart the selection was made in is filtered by it too, exactly as the
canvas drew selections before round 3 (so a client that does not draw `highlights` never shows a
full bar as if it were the selected part). With the flag the server filters the targets that
filter, and for a target that highlights (only column, bar, pie and donut charts, including the
stacked and 100% variants: `interactions` says `highlight` for exactly those) it keeps all its
rows and adds `highlights`: the same rows with each measure as the selection highlights it (null:
nothing of that row is selected). `highlight` is part of the data key.

```json
{ "id": "c", "rows": [["Consumer", 10.0], ["Corporate", 6.0]],
  "highlights": [["Consumer", 4.0], ["Corporate", null]] }
```

Draw the full value dimmed and the highlighted value in full color. With the flag, the chart the
selection was made in gets `highlights` for its own selection too (Desktop dims its other points)
and keeps all its rows.

A slicer in a sync group has `syncGroup: { groupName, fieldChanges, filterChanges }`, and the page
response's `sync_groups` lists every slicer of each group across the report (`{ page, visual,
hidden }`). `filterChanges` true means the group shares its selection: when the user selects in
a synced slicer, send the same values as the selection of the group's slicer on every page you
render (a hidden synced slicer still filters its page). With `filterChanges` false the slicers
only share their field (`fieldChanges`: a field change made in one applies to all, an editing
concern): keep each page's selection separate.

## 20. Phone layout

`mobile` is `{ visuals: { <id>: { x, y, width, height, z } } }` when some visual of the page is
placed on its phone layout (`mobile.json` beside the visual), else null. For a phone view, draw
only the listed visuals at those positions on a 320 px wide canvas, stacked by `z`; the visuals
themselves are the same rows as on the desktop layout.

## 21. Visual calculations, small multiples, tooltip fields and dynamic titles

- **Visual calculations** (`NativeVisualCalculation` projections) are `kind: "computed"`,
  `computed: "visualCalculation"` columns in their well position. Studio computes running sum,
  moving average, previous, next, first, last, versus previous/next/parent/grand total, percent of
  parent and of grand total, and percent change over the visual's axis (ascending by its category
  fields, or by the column the model sorts a category by: Month by MonthNumber) from the rows it
  has; any other expression is blank and the visual `approximate`. Their `format` is the source
  field's (a percent of total is `0.00%`). Percent of grand total and versus grand total divide by
  (subtract) the measure's value for the whole visual, computed by the engine in the second query
  (right for an average too, not a sum of the rows); if that piece fails they are blank and the
  visual approximate. With one category level the parent is the grand total and is computed the
  same way; with two or more levels the parent is summed from the rows, which is right only for
  a measure that adds up, so the visual is `approximate`.
- **Small multiples**: a chart (or new card) split by a field has
  `formatting.smallMultiples: { index, field, query_ref, layout: { type, rows, columns } }` (the
  field is the column at `index`; `rows`/`columns` null for Power BI's automatic grid) and, as
  data, `smallMultiples: { index, field, values, panels: [{ value, rows: [row indexes] }] }` in
  ascending panel order. Draw one small chart per panel from its rows. The 100-row limit applies
  to all panels together (`truncated`).
- **Tooltips well**: its fields are queried but `hidden` (Desktop shows them on hover only).
- **Dynamic titles and backgrounds** (a measure gives `title.text`, `title.fontColor`,
  `title.background` or `background.color`): `formatting.dynamicProperties` lists the paths and
  the data key `dynamic` holds the values for the whole visual, e.g. `{ "title.text": "Sales 2026",
  "title.fontColor": "#00FF00" }` (text is plain, colors validated). Use them instead of the
  static token; `title.textSource` stays `dynamic`. Every visual, a card too, gets them from its
  whole-visual piece of the second query (the helper's cells in `rows` are null); if that piece
  fails `dynamic` is absent, the paths are in `unrendered` and the visual is approximate. A
  visual with a measure filter gets none and is `approximate`.

## 22. What stays approximate, and the shapes to verify in Desktop

- Computed over the rows Studio draws (at most 100): a gradient's lowest and highest value,
  percent rule bounds, analytics lines other than constants, running and moving visual
  calculations and small-multiple panels. `formatting.rangeDependent` is true when a visual has
  one of these; when such a visual is also `truncated` its data carries the note "Only the first
  100 rows were used for the values computed here" and it is approximate.
- Not applied: a bookmark's sort, drill and field choices (when they differ from the visual's),
  spotlight, focus mode and layer order; the source page's filters of a drillthrough or tooltip
  opened without `drill_source`; a field parameter's other choices; a percent of parent over two
  or more levels; a trend line per legend series; error bars, forecasts, shading, anomaly
  detection; formatting and data bars on totals.
- `approximate` visuals list what they miss in `unrendered`, say why in `dataNotes` or the page's
  `notes`, or were marked by the rules above.
- Shapes read from the published schemas and from what the editing families found in Desktop
  2.157's own code, not from a file Desktop saved, are listed in the modules' `UNVERIFIED_SHAPES`
  (`render_rules`, `render_analytics`, `render_navigation`, `report_tokens`, `live_report`); the
  Desktop probes of the editing families decide them.

## 23. Round 3 fix pass: what the client must know

Request (section 1):

- `highlight: true` opts into cross-highlighting (section 19). Without it a chart selection
  filters every target and the source chart, as before round 3. Send it only once the canvas
  draws `highlights`.
- `drill_source: { page, visual?, selections? }` carries the source page's context of a
  drillthrough or tooltip (section 18). `drill` takes up to 24 fields.

Page response (sections 1 and 10, both render routes):

- `notes: string[]`: page-level reasons the data visuals are approximate (today: the source
  page's filters missing). Show them once, near the page title.
- `drillthroughs: [{ page, title, fields, keepAllFilters, crossReport }]` for a "Drill through"
  menu; `page.binding` gained `crossReport`.

Visual row (section 2):

- `dataNotes: string[]` (data key): the visual is approximate for a reason only its data shows:
  "Only the first 100 rows were used for the values computed here" (truncated and
  `rangeDependent`), "The totals, highlights or whole-visual values of this visual could not be
  computed here" (the second query failed for it; the lost parts are in `unrendered`: `total.totals`,
  the `dynamic` paths, `<line object>.value`).
- `declined: true` (data key): Studio's engine left the visual to Power BI (section 8).
- `formatting.rangeDependent: bool` for data visuals (section 22).
- Every column carries `kind, table, name, label, role, query_ref, format, alignment, colorIndex,
  hidden` (section 2). A new `role: "SortBy"` hidden column orders a category, and `sort[].index`
  may point at it.
- A whole-visual helper (dynamic title, measure line) is null in every row; read `dynamic` or
  `referenceLines`.
- Several `trend` entries (one per measure, with `series`) when "Combine series" is off.
- New fidelity causes: a calculation group slicer's selection (`needs_desktop` on the visuals it
  reaches), a table that may be a calculation group or field parameter while the model facts are
  unread (`needs_desktop`), a chart's inactive drill levels are no longer queried (only the level
  its axis shows: the active projections, else the first).

Merging a formatting-only row (section 10): the data keys are now `rows`, `formatted_rows`,
`selected`, `truncated`, `sort`, `extra`, `totals`, `highlights`, `referenceLines`,
`smallMultiples`, `dynamic`, `dataNotes` and `declined`. Keep them from the cached row; a cached
`declined` row keeps its `error`, `needs_desktop` and `fidelity: "none"`, and a cached row with
`dataNotes` stays at least `approximate`.

The data key now also moves with the theme's palette (a rule using a theme color), `highlight`,
`drill_source` and the model facts' calculation groups and field parameters.

Buttons and navigators (section 16): state precedence, the disabled rules (`valid: false`; a
drillthrough button until a point is selected), styled states arrive complete, `navigator.kind`
`bookmarks`, and `qna`/`unknown` actions (enabled, click says "Power BI only").

Bookmarks (section 17): the filter states saved by name (locked or hidden filters) and by type
(include/exclude) are applied too; one Studio cannot apply adds a bookmark note and makes the
visuals it reaches approximate. The sort/drill note appears only when the saved state differs
from the visual's, and spotlight, focus mode and layer order get their own notes.
