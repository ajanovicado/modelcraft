# Model storage modes

Opening a PBIX in Studio does not convert its semantic model to Import mode.
Storage is identified from the current model's table and partition metadata,
independently of whether Studio could decode saved rows from the file.

| Mode | Meaning | Studio calculation route |
| --- | --- | --- |
| Import | Model rows are cached and updated by refresh | Saved snapshot, when available and the expression/model features are supported |
| DirectQuery | Power BI queries the configured source | Exact working-copy Power BI Desktop model |
| Dual | A table can use cache or DirectQuery according to the query | Power BI model, even when cached rows exist |
| Hybrid | One table contains Import and DirectQuery partitions | Power BI model; saved rows can cover only the Import portion |
| Direct Lake | Power BI reads data in OneLake | Power BI model; Studio does not reproduce this from saved rows |
| Unknown | Metadata is absent, unsupported or incomplete | No numeric result from that table's cached rows |
| No local model | The report has no embedded local semantic model | Confirm its connection in Power BI; absence alone does not prove a working live connection |

A composite model can combine storage modes or different source groups. Mixed
table modes are visible in Studio. PBIX extraction does not currently inspect all
source groups, so an all-DirectQuery summary does not rule out a composite model.
A live connection is a report connection to a remote semantic model, rather than
a table storage mode. See Microsoft's [storage modes](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-storage-mode)
and [composite models](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-composite-models).

## Simple interface

In an imported report's **Model** tab, **Model storage** starts collapsed. Open
it for table modes, saved-row coverage and calculation guidance. Table badges and
the selected table's note retain that distinction without expanding the summary.
Unknown or reading states do not reuse stale mode labels. Known table modes may
remain visible when the overall model inventory is incomplete.

The view is read only for storage modes. Configure connections, storage changes,
incremental refresh and source credentials in Power BI. Studio does not create a
remote connection or convert a table by changing its label.

## Calculation and assistant behavior

The local DAX engine resolves omitted/default partition modes only against an
explicit known model default. It identifies Hybrid from an Import/DirectQuery
partition mix, retains unrecognized modes as unknown, and refuses cached-row
calculations on native-only or incomplete tables. The same restrictions apply to
grouped DAX previews, imported visual calculations and computed chat answers.
An unrelated native-only table does not disqualify a supported Import expression.

Workspace chat receives bounded mode facts beside its schema. DAX Lab uses the
current model's names and distinguishes a formula proposal from execution
evidence. Unknown storage cannot be inferred from cached rows. Saved row counts
describe Studio's snapshot, not current source freshness or full model coverage.

For supported business patterns on recognized native-only tables, current names,
types, dependencies and relationship metadata can ground a formula proposal.
It remains **Not validated** until executed in Power BI; Studio does not attach
calculated values or register it as a reusable local preview. Incomplete or stale
metadata cannot supply those proposal choices. This flow currently requires a
workspace snapshot, even when the formula's referenced table has no usable local
rows. Native execution of a supplied formula can work without that snapshot.

Reports imported before model metadata was recorded can retain the legacy
snapshot evaluator until their metadata is read again. Those saved-snapshot
results do not verify storage mode, full coverage or freshness. Re-import such
reports before relying on native model behavior. Current imports record a model
read attempt and refuse local results when that metadata cannot be identified.

Native DAX requires the exact working-copy Desktop process and its single
queryable local model engine, with the current report revision placed in Desktop.
Missing, ambiguous or stale engine identity produces an actionable error. A thin
report may have no local queryable engine; Studio has no remote XMLA fallback.
Native querying does not itself refresh the source.

Existing PBIP compatibility inspection follows the selected report's structured
semantic-model reference. It reads storage properties rather than guessing from
comments, formulas or sibling projects. DirectQuery, Dual, Hybrid, Direct Lake,
live and composite models remain outside managed Import-project migration.
Inspection does not alter a source project.

## Verification and release scope

Automated checks use isolated files, fixture metadata, fake model/Desktop
connections and mocked browser requests. They cover mode inheritance, unknown
future modes, Hybrid partitions, stale model caches, grouped previews, current
schema grounding, native process isolation and UI state at 1440 and 1280 pixels.
They do not qualify an authenticated DirectQuery, Direct Lake or live connection.

Before advertising a mode at release, compare actual Desktop and Studio results
with approved source access. Include mixed storage and mixed-source models,
fresh/stale caches, incremental-refresh boundaries, role permissions, credentials
and reconnect failures, two open reports, and the installed application. Keep
Power BI connector, gateway and license requirements explicit. See the
[source qualification guide](data-source-connections.md) and
[release checklist](release-checklist.md).
