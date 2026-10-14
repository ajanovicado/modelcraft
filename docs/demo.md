# Three-minute Modelcraft walkthrough

## Launch

```powershell
cd apps/desktop
npm ci
npm run demo
```

Open the printed URL. The actual React interface uses an in-memory synthetic
retail fixture. Assistant replies are fixed examples; no model or external
database is contacted. It demonstrates the product without downloading weights
or opening personal reports.

## Walk through the workspace

1. **Report:** show the dashboard and conversation. Explain how a selected
   visual supplies context and where review/build creates the approval boundary.
2. **Data:** inspect rows and profiles. Explain preserved source bytes,
   normalized snapshots and preview filters.
3. **Model:** inspect tables and relationships. Discuss keys, ambiguous joins
   and why unsafe relationships block a build.
4. **DAX:** inspect the example formula. Separate syntax/static checks from
   numerical results that require the native Power BI engine.

The fixture illustrates these screens. Fixed replies and demo actions do not
prove model accuracy, native DAX results or Power BI rendering. Live operation
and installed releases have separate qualification requirements.

## Reproduce the gallery

```powershell
cd apps/desktop
npm ci
npx playwright install chromium
npm run screenshots
```

The helper renders the synthetic fixture at a desktop viewport, checks browser
errors and writes four showcase PNGs to `docs/images`. It uses dedicated loopback
ports and does not open saved workspaces, import personal data or run a model.

## Opening explanation

“Modelcraft brings data, model, report and assistant into one workspace.
The assistant proposes bounded plans. Deterministic code owns execution and
validation, and users review report changes before applying them.”

Continue with one implementation decision, a failure-path test and a limitation.
See the [interview walkthrough](interview-walkthrough.md).
