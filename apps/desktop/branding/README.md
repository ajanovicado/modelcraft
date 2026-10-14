# Modelcraft branding

Modelcraft — Your AI workspace for Power BI.

The interlocking M represents building connected data models. Dark ink and warm gold tie the identity to the app's existing palette. The final icon uses a light background for visibility on light and dark desktop surfaces; the wordmark remains native, accessible text in the header.

`modelcraft-icon-master.png` is the original 1254 × 1254 raster master. The public PNG files are resized exports used by the app, favicon and touch icon. Native icons are exported by the installed Tauri CLI, including multi-resolution Windows ICO and ICNS. The retired bar-chart SVG was removed; the generated master is a raster asset, not an editable vector logo.

To regenerate the native exports from the public 512px PNG, run from `apps/desktop`:

```powershell
npm exec -- tauri icon ./public/branding/modelcraft-icon.png --output ./src-tauri/icons --ios-color '#FAFAF8'
```

Generation mode: built-in ImageGen. Final prompt, used for a new image with an opaque background:

> Create a final standalone square app icon for Modelcraft, a professional Power BI modeling workspace. The canvas is solid warm white #FAFAF8, full bleed to all edges. One large precise flat geometric M symbol centered with 18% clear padding. The M has two dark ink #17212B vertical legs, angular tops descending diagonally to a sharp centered V at 62% of icon height, and a gold #F4C522 diagonal band running from that center V up to the top right. The right leg below the gold band is ink. The gold band and ink left plane join exactly at the vertical centerline. The two legs end in angled cuts, with the far left/right corners lowest. This suggests an interlocking constructed model. Absolutely flat solid fills, mathematically straight clean edges, extremely simple polished vector-like graphic. No rounded tile, external transparency, gradient, shading, texture, border, flecks, tiny decoration, shadows, 3D effects, text or extra symbols. Strong silhouette readable at 24px. Output one clean production icon, no mockup or options sheet.

Technical package, sidecar, bundle and storage identifiers retain their established names to preserve existing data. MSI retains the immediately previous UpgradeCode. Migration from older development installers and NSIS installations requires installer qualification.
