# Client Demo Design System

The visual concept is saved at `design/app-concept.png` and is the implementation
reference for the Streamlit experience.

## Intent

Calm enterprise confidence: generous white space, compact grounded evidence,
and visible safety without developer-console clutter. The application previews
plans and evidence; it does not imitate a Power BI dashboard.

## Tokens

| Token | Value | Use |
|---|---:|---|
| Canvas | `#F6F8FB` | App background |
| Surface | `#FFFFFF` | Cards and main workspace |
| Ink | `#122033` | Primary text and action |
| Muted | `#637083` | Secondary copy |
| Border | `#E3E8EF` | Quiet grouping |
| Cobalt | `#356AE6` | Primary data series and selected state |
| Emerald | `#138A63` | Grounded/validated status |
| Amber | `#D98B19` | Comparison and assumption callout |
| Danger | `#C94040` | Blocking failures only |

Use a restrained 12 px radius, subtle shadow, tabular numerals for values, and
a modern system sans stack. Headings should be short and sentence case.

## Information hierarchy

1. Workspace and freshness.
2. User request and compact interpretation.
3. Clear Review → Apply action.
4. Grounded evidence with exact values and provenance.
5. Context rail with pages, filters, assumptions, and trust state.

## Release acceptance behavior

These are requirements to verify against the frozen release revision. They do
not, by themselves, claim that the current browser build or target Windows host
has passed timing or interaction QA.

- A long-running job emits immediate status and a progress event at least every
  five seconds.
- Disabled actions explain why.
- “Validated locally” must never be labelled “render verified.”
- Numerical answers always expose metric definition, period, filters, data
  cutoff, and query provenance.
- The evidence addition names its destination and replaces only the reserved
  slot.
- Developer diagnostics are behind an explicit expander.
