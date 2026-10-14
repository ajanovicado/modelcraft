# Assistant model and relationship behavior

The assistant uses the current workspace schema and reviewed model facts. A
relationship request must match the specified tables, columns, activity,
cardinality and filter direction. A different valid relationship cannot stand in
for the requested one. Unknown or ambiguous keys produce an actionable response
and leave the approved draft unchanged.

Join-only requests use a compact candidate-selection schema that cannot create
pages, visuals or measures. Requests that also ask for report additions retain
the full revision planner and its requested-count checks. Every accepted model
proposal still passes deterministic validation before the draft changes.

## Managed snapshot models

Full snapshot scans establish key uniqueness, coverage and supported filter
paths before the LLM can recommend a candidate. Managed one-side keys must be
unique and nonblank. Duplicate lookup keys, inactive paths and ambiguous active
paths cannot produce dashboard totals. These checks run again when executing a
preview, so an earlier approval cannot hide changed key data.

Imported text keys compare without case and trim trailing spaces, preserving
leading spaces. DateTime keys retain their full timestamps. Unmatched fact rows
remain visible in the blank group instead of disappearing from totals. These
rules apply to supported snapshot calculations; DirectQuery source collation
is not inferred from an Import snapshot.

Draft dashboard joins and managed DAX currently require a direct approved active
relationship. Imported snapshot DAX can traverse supported active many-to-one
chains. Bidirectional, many-to-many, one-to-one and explicit USERELATIONSHIP or
CROSSFILTER calculations require Power BI execution rather than a guessed result.

## Existing Power BI models

The imported-model assistant receives bounded current relationship settings,
storage modes and revision identifiers alongside schema names. It does not
receive partition queries or source row values through this model context.
Relationship edits retain exact endpoint identity and use effective existing
settings during preflight and native planning. Reversed endpoint requests map
cardinality to the model's actual stored ends. One-to-one relationships require
bidirectional filtering; security filtering cannot be silently reset.
Changes that would reverse a regular native relationship's physical ends while
retaining Single filtering are refused until configured in Power BI, or the user
explicitly requests a supported Both configuration. Fake native metadata tests
do not establish real filter propagation.
For many-to-many Single filtering, an explicitly requested direction must match
the stored To-to-From direction. Opposite or unknown directions require
clarification rather than an implicit direction change.

Enabling Assume referential integrity requires verified source-group and key
integrity facts that this imported structure inspection does not currently
provide. The assistant refuses that change with Power BI guidance. Disabling
the setting is supported; valid existing settings can remain during unrelated
edits. Many-to-many relationships cannot retain the setting.

See [model storage modes](model-storage-modes.md) for Import, DirectQuery, Dual,
Hybrid, Direct Lake and unknown-mode execution boundaries. Changing a mode label
does not connect a source or convert its storage.

## Validation scope

Regression tests cover adversarial LLM outputs, exact requested keys/settings,
lookup duplicates, inactive and ambiguous joins, text and DateTime keys,
physical blank and unmatched keys, multi-hop filtering, native endpoint
orientation and same-batch model updates. Isolated real-Qwen evaluations are
recorded separately from deterministic or fake-model tests. A fallback after a
failed model call is not counted as successful inference.

Synthetic tests and compiled plans do not qualify authenticated live sources,
Direct Lake or every installed Power BI Desktop model. Those require the
[source qualification](data-source-connections.md) and
[release validation](release-checklist.md) flows.

The model rules follow Microsoft's documentation on
[relationships](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-relationships-understand)
and [Import text types](https://learn.microsoft.com/en-us/power-bi/transform-model/desktop-data-types).
