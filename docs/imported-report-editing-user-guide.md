# Editing an imported dashboard: a guide for Power BI users

When you import a `.pbix` file, Studio does more than show it. You can change it the way you would in Power BI Desktop, by
typing what you want in the chat or by clicking on the page, and every change can be undone. Your original file stays exactly
as it was until you decide to save into it.

## What you can change

- **Visuals:** size and position; add,
  delete, duplicate, hide, reorder, align and space them out; change a visual's type and the fields it uses; sort; add a text
  box or a shape.
- **Pages:** add, rename, duplicate, delete, reorder, hide and resize.
- **Filters:** on a visual, on a page or on the whole report.
- **Measures:** create, change, rename and delete (these need Power BI Desktop; see below).
- **Conditional formatting and number formats:** color scales, rules, colors from a field, data bars and icons on a table,
  matrix, chart or card; display units, decimals, currency and percent formats; table and matrix totals,
  subtotals, structural layout and column order.
- **Navigation:** bookmarks (add, update, rename, group, reorder, delete, hide a visual in one), buttons and their actions
  (back, bookmark, page, drillthrough, web link), page and bookmark navigators, drillthrough and tooltip pages, edit
  interactions, synced slicers, and how drilling behaves.
- **More visual features:** analytics lines (constant, average, min, max, median, percentile, trend, forecast, error bars,
  ratio and shading), the phone (mobile) layout, grouping visuals, visual calculations, small multiples,
  alt text and tab order, keeping a visual's layer order, the page view, and renaming a field in one visual.
- **The data model:** calculated columns and tables, column properties (format, sort by, category, summarization, folder,
  hidden, key), renaming and deleting columns and tables, relationships, hierarchies, the date table, field parameters,
  calculation groups, roles (row-level security), query parameters and data refresh. These need Power BI Desktop, like
  measures.

## Two ways to make a change

**Say it in the chat.** Select a visual first (click it on the page) and words like "it" and "the title" mean that visual.
Otherwise name it: "the Agents Avg MTD Sales card", "the top left chart", "every table".
An exact visual title narrows the change to that visual; use both titles when swapping two charts.
Name a chart for number formats, and a table or matrix for field headers and structural table layout.

**Click.** Click a visual to select it (Ctrl-click adds more; hold Alt to reach the one underneath). Drag it to move it, drag
a corner or an edge to resize it, or use the arrow keys (Shift makes the step bigger). The panel on the right has the details:

- **Visual:** size and position, number formats, conditional formatting and structural table layout.
  A setting tagged "Shown in Power BI" is saved but Studio's preview does not draw it.
  A column's "Header text" renames that field in this visual only.
- **Fields:** what the visual shows, by role. Add, replace or remove a field, change the visual type or the sort.
- **History:** everything you changed, with Undo (below).

The list **Visuals on this page** above the panel does everything the mouse does, without one: select any visual (hidden ones
too), show or hide it, bring it forward or send it back. The page tabs on the left add, rename (double-click or F2),
duplicate, delete, move (drag, or the up and down buttons), hide and resize pages.

Studio preserves and draws a report's existing styling. To change colors, fonts, titles, labels, legends,
axes, borders, backgrounds or button appearance, use Power BI Desktop's Format pane. Studio refuses
these styling requests without applying a substitute change. New CSV and Excel dashboards also have
no theme preset picker.

## Things you can type

1. `Move Total MTD Sales to x 200`
2. `Show the data labels in millions with 2 decimals` (changes their number format)
3. `Make the card bigger` (with the visual selected)
4. `Align the charts on the left`
5. `Show the total row in the agents table`
6. `Bring the TopN/BottomN card to the front`
7. `Make it a line chart` (with the visual selected)
8. `Add a bar chart of Total Sales by channel`
9. `Rename page 3 to Mix` or `Move Forecast to the front`
10. `Move Rank to be the first column`
11. `Create a measure called Profit Margin as DIVIDE([Profit],[Total Sales])` (asks before it does anything, see below)
12. `Undo`, or `Restore the original`, or `What did you change`

You can put several changes in one message ("make the chart wider and move the table left"). They become one
change, so one Undo takes all of it back. When Studio makes an AI plan alongside clear report changes, those changes wait
in the same plan for Apply. For other unclear or unsupported parts, Studio can do the clear parts, ask about the rest and
tell you which was which.

For example, `Move the ChannelName slicer to x 200 and show the Total MTD Sales bar labels in thousands with 2 decimals`
can be reviewed as a two-step plan and applied together. One Undo takes both changes back; one Redo restores both.

"What if the target were 120K?" is a question, and Studio answers it without changing anything. "Change the target to 120K"
is a change, and Studio asks you to confirm it first.

## More things you can type

**Conditional formatting, number formats and tables**
- `Shade the Rank column background from light to dark blue` (a color scale)
- `Shade the vs PM % cells red below 0 and green from 0 up` (rules), `Add data bars to the Rank column`, `Show three
  colored arrows next to vs PM %, on the right` (icons)
- `Show the Total MTD Sales data labels in thousands with one decimal`
- `Show the total row in the agents table`, `Move Rank to be the first column`
- `Remove the color rule from the Rank column`. If a column already has rule colors, Studio asks before replacing them.

Colored up, right and down arrows appear beside table values in Studio's preview. Allow the data preview to refresh
after Apply; Undo and Redo also update the displayed icons.

**Bookmarks, buttons and pages**
- `Add a bookmark called Overview`, `Update the Overview bookmark`, `Hide the table in the Overview bookmark`
- `Add a back button`, `Make the Details button go to page Stores`
- `Add a page navigator` or `Show the bookmark navigator as a 2 by 2 grid`
- `Add a bookmark navigator on the Forecast page`. "Bookmark" chooses what it lists; the page name chooses where it goes.
- `Make Agent details a drillthrough page on the agent`, `Make Spare a tooltip page`, `Use Agent tooltip as the tooltip of
  Total MTD Sales`
- `The Country slicer should not filter Total MTD Sales` (edit interactions), `Sync the store slicer to page Stores`
- Deleting a bookmark that a button uses, or turning a drillthrough page back into a normal page that visuals point at, asks
  first and then removes those links too.

A back button uses Power BI's Back action and needs no destination page. To sync a slicer, name the slicer on its source
page and the destination page. Studio joins a matching slicer there to the same group, or adds a synchronized copy if
there is none. Making a tooltip page sets that page's type; using it as a chart's tooltip is a separate request.

**Analytics, phone layout and other features**
- `Add a dashed red target line at 50,000 to the Total MTD Sales chart`, `Add a trend line`, `Add a forecast`
- `Build a phone layout for page 1` or `Put the Store slicer at the top of the phone layout`
- `Group the four cards and call the group KPI cards`, `Ungroup the KPI cards`
- `Add a running total of MTD Sales to the agents table` (a visual calculation), `Split the Total MTD Sales chart by channel`
  (small multiples)
- `Set the alt text of the agents table to Agents ranked by MTD sales`, `Make the slicers the first tab stops`
- `Fit page 1 to width`, `Rename MTD Sales to Sales this month in the agents table`

For a visual calculation, use the field header you see in the table, including a renamed header. You do not need to know
the field's original model name. For filters and color rules, state the value or cutoff explicitly, such as the Store
channel or "below 0". When Studio can form a valid plan, it fills in these stated values if the AI leaves them out.

**The data model** (each asks you to confirm, then goes through Power BI Desktop)
- `Add a Margin column to FactSales that is SalesAmount minus TotalCost`, `Sort the Month column by Month Number`
- `Rename StoreName to Store` (the visuals and formulas that use it are updated), `Hide the DimChannel table`
- `Make the store relationship filter both ways`, `Create a Date Hierarchy on Date: Year, Quarter, Month`
- `Mark Date as the date table on Date`, `Create a field parameter Sales Metric with Employees Avg MTD Sales and Table MTD
  Sales`
- `Create a calculation group Time Intelligence with Current and YTD`, `Create a role Store Managers that only sees stores of
  type Store`
- `Set the parameter Region to East`, `Refresh FactSales`, `Discourage implicit measures`

If several tables have the same column name, include its table: `Hide Orders[OrderKey] in the model`. Otherwise Studio
asks which table you mean and offers a choice for each one.

**When the AI plans a change.** A request the chat cannot read word for word (for example "add a running total to the table")
goes to the local AI, which picks from Studio's list of changes and fills in the details. Studio then shows the plan: one line
per step, which steps need Power BI Desktop, and anything it left out. Nothing happens until you press **Apply**; **Cancel**
leaves the report as it was. An applied plan is one change, so one Undo takes it all back. If the AI is unsure, it applies
nothing and offers a few suggested changes instead.
A plan leaves out extra renames and custom format strings that your request does not specify. Check the shown steps
before applying them.

You can also type `yes`, `go ahead` or `apply` to answer the latest open confirmation, or `no` or `cancel` to leave it.
This works for an Apply/Cancel question, including a plan or model change, for up to 30 minutes. A new message ends that
shortcut, although the question's buttons still work until it expires. A question with several choices needs a choice
button.

**Working with formulas.** Describe a new calculation in words and the local Qwen model can draft it for you. Studio checks
the draft locally before offering it to apply. The AI is the local model on your computer: nothing is sent to an
external service. It can read your report's DAX and formulas you type, so it can explain, fix and refine them. Power
Query (M) code stays out of its prompts.

For example, `Create a measure called Discount Share that is total DiscountAmount divided by Total Sales` gives both
the name and the calculation. Giving only a name makes Studio ask what it should calculate. Review the draft before
applying it to the report; an explicit formula you type is checked locally.

In DAX Lab, Explain, Fix error and follow-up requests ("make this year to date", "only for West") send the formula in
the editor and the recent conversation to the local model, which explains or rewrites it. Studio checks every rewritten
formula on your data before showing it. You can also edit the formula yourself and press **Run** to check it and see
the result.

## After every change

Studio tells you what it did in plain words, and adds three kinds of note when they apply:

- **Visible in Power BI only:** the change is saved, but Studio's preview cannot draw it. Check it in Power BI.
- **Kept as it was:** existing formatting that an edit leaves alone.
  Studio asks before replacing an existing conditional-format rule.

## Undo, Redo and History

- **Undo** takes back the newest change, from the toolbar, from the change's own card in the chat (`Ctrl+Z` works on the
  page too), or by typing `undo`. **Redo** puts back what an Undo took away.
- Undo only ever applies to the newest change. For anything older, open **History** and choose **Restore to before this**.
  Studio first lists the later changes that would be left out, including any you made in Power BI Desktop, and asks you to
  confirm. Nothing is deleted: the restore is itself a step in History.
- History keeps the most recent versions. A row marked "Not kept on disk" is older than that and can no longer be restored.

## How Power BI Desktop is involved

Report-layer changes happen inside Studio: layout, pages, fields, filters, number formats,
conditional formatting, bookmarks, buttons, interactions, analytics lines, groups and the phone layout.

**Model changes need Power BI Desktop.** Creating, changing, renaming or deleting a measure, and every change to columns,
tables, relationships, hierarchies, field parameters, calculation groups, roles, queries or the data refresh, changes the data
model, and only Desktop can do that. In chat, the confirmation or plan offers **Apply to report**. A measure confirmation
also offers **Add to pending changes** so you can apply several measures together. Changes prepared in the model editor or
DAX Lab wait in **Pending Power BI changes**, with the formula and any local "what if" answer, until you press
**Apply to report**. Then Power BI Desktop opens briefly, makes the confirmed changes, checks them and saves, which takes
about a minute. While it runs, please leave Desktop alone: Studio
shows the steps and closes it for you. (When you press **Open the edited copy in Power BI**, Studio also sets its local AI
aside while Desktop starts, so the next question you ask it starts a little slower.)

- If Desktop is already open with **unsaved changes** of yours, Studio never saves or closes over them. It asks: **Save into
  history and continue** (your Desktop work becomes a step in History, then Studio carries on) or **Cancel**.
- If Desktop is showing a dialog, Studio waits and tells you which one.
- Studio never forces Power BI Desktop to close. If it cannot close it safely, it leaves it as it is and tells you.
- If a measure is still used by a visual or another measure, Studio refuses to delete it and names where it is used. The
  same goes for a column or table that visuals, filters, formulas, relationships or hierarchies use. If a change fails,
  nothing is kept and your report is as it was.
- Microsoft documents only some model changes as safe for tools like Studio to make while Desktop is open (measures,
  calculated columns and tables, relationships, field parameters, calculation groups, roles, column properties). Renaming
  columns and tables, hierarchies, the date table, queries and refresh are made only once Studio has checked them on your
  version of Power BI Desktop; until then Studio says so and gives you the steps to do it in Desktop.
- If reloading the data with a new query fails, Studio undoes it and asks whether to keep the new query without reloading.

**Open the edited copy in Power BI** opens Studio's edited version so you can see the real thing. It is a working copy inside
Studio's own folder, not your original file.

## Save a copy or Save to file

Studio shows a status bar such as "Edited in Studio · 3 changes not in your file".

- **Save a copy…** writes the edited report to a new `.pbix` file. By default the copy is named after your file with
  "(edited)" added, next to it. An existing file is never overwritten: a number is added to the name instead. This is the safe
  choice, and the only one offered when your original changed after you imported it.
- **Save to file…** replaces your original file. Studio names the file and asks you to confirm, keeps a backup of the current
  file next to it first (`<name>.pbix.studio-backup-<time>`), and refuses if the file changed since you opened it here or is
  open in Power BI. Choose **Save a copy** if you are not sure.

A file Studio writes opens normally in Power BI. Power BI adds its own bookkeeping the first time you save it there.

## What Studio cannot do yet

When you ask for one of these, Studio says so and gives you the steps to do it in Power BI Desktop. Nothing is attempted or
half-done. Open your edited copy in Power BI, do the step, and save.

| You asked for | Where to do it in Power BI Desktop |
|---|---|
| Colors, fonts, titles, backgrounds, labels, legends, axes, button appearance and format painting | Select the visual or page, then use the Format pane |
| Images and logos | Insert, then Image |
| Locking objects on the canvas | View, then Lock objects (Desktop keeps this switch in the view, not in the report) |
| Rich text in a text box (bold words, lists, links) | Click into the text box and use its floating toolbar |
| Adding people to a role | The dataset's Security page in the Power BI service, after publishing |
| Merging, appending or reshaping queries | Home, then Transform data (Studio changes query parameters and refreshes data) |
| Custom visuals | Get more visuals in Power BI Desktop |
| Publishing and sharing | Home, then Publish (use Save a copy here first) |
| Hiding or unhiding a measure | Right-click the measure in the Data pane, then Hide or Unhide |

Other limits:

- Reports saved in Power BI's older layout format can be opened and read, but not edited. Studio says how to upgrade one: open
  and save it once in a current Power BI Desktop.
- A report that reads its data from a shared model can be laid out and given supported report-layer edits, but Studio cannot preview its data or
  change its measures. Check its changes in Power BI.
- Some visual types (gauge, treemap, waterfall and others) are only drawn as a frame in Studio's preview, marked "Preview in
  Power BI". Editing them still works.
- The chat only edits what it can read with certainty. When it cannot, it applies nothing and offers a few suggested changes
  to pick from.

## Translating business questions

In chat or DAX Lab, describe the calculation and use the names of fields in your model. For example,
"Create a measure called Discount Share that is total DiscountAmount divided by Total Sales."
Studio binds the numerator and denominator separately, builds the formula from a reusable pattern,
and computes a local preview. Read the displayed value and scope before saving or applying it.

The pattern library covers ratios, share of total, month/quarter/year to date, year-over-year change
and percentage change, running totals, moving averages, rank and top N, distinct counts, average
per entity, actual versus target, and previous periods. Ambiguous fields, missing calendar
relationships or unspecified comparison scope require clarification. The local AI can select
numbered fields and pattern options; Studio writes the pattern's DAX. Other calculations retain
the existing freeform drafting path.

Year-over-year comparisons distinguish an absolute difference from a percentage change.
Moving averages currently average daily values; requests for a weekly, monthly or quarterly
average require clarification. A window measured in months can still average daily values.

Pattern previews use the current imported data snapshot and edited model. They do not inherit
temporary canvas selections or page slicers. A blank result is reported as blank. Table previews
show at most five rows; top N queries belong in the query editor, rather than being saved as a
scalar measure. A computed preview does not itself approve or change the report.

For CSV and Excel drafts, compiled scalar calculations still need advanced DAX review and
dashboard approval before they become approved measures. A generated virtual Calendar can
support a preview, but saving a time-intelligence measure requires a reviewed source date
table and a governed relationship. Table queries cannot be saved as scalar measures.

For an existing report, try "Walk through this page", "Explain this chart" with a selected visual,
or "Walk me through this measure" with a selected measure. Definition walkthroughs describe saved
fields, calculations and filters; current number explanations use the local engine when available.
Ask "Which chart should compare Total Sales by ChannelName?" for a recommendation with the actual
fields. Ask for relationship, date-table or star-schema advice to see existing model facts and
checked relationship candidates. Recommendations and model advice leave the report unchanged.
Explicitly named pages, visuals and measures take precedence over an attached selection.
Questions about several named measures use the existing local AI explanation path, preserving
the whole question. A data-model walkthrough includes existing relationships, unmatched keys
and disconnected parameter tables when that information is available.

## Privacy

Everything stays on your computer. Your report is copied into Studio's own folder on this machine; the edits, the history and
the previews are stored there. The AI that reads your chat messages runs locally, and Studio does not send your report, your
data, your messages or anything else to any online service. Power BI Desktop is started only from your own installation,
and only when you ask for something that needs it. Existing and user-supplied DAX can be read by the local AI to explain,
fix and refine calculations. Power Query (M) bodies stay out of its prompts. Formula evidence is stored as hashes.
Your original file
is touched only if you choose **Save to file…** and
confirm. The copies and the history of an imported report are stored in Studio's own folder for as long as you keep it.

## Verification and limits (2026-10-02)

The prior 40-case live-model result overstated correctness: its scorer accepted nonempty formulas
and named roles without verifying what they computed or filtered. The corrected 2026-09-30 full live
run used 109 cases: 59 of 73 plans were correct and 33 of 36 negative requests produced no plan.
Three plans remained unverified. This was a development set and is not a general accuracy guarantee.

The styling removal is checked by deterministic parser, gold-stub planner, noisy planner and coverage
evaluations. Those checks do not establish live-model quality. Existing Desktop probes and chat-box
checks are historical evidence; the current release needs its own live smoke check. Tooltip assignment
and interactive filtering between synced slicers were not established by the earlier chat-box checks.
