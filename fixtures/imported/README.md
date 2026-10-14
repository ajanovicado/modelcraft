# Imported-report fixtures

Test PBIX files for the imported-dashboard editor (`pbix_package`, `pbir_document`, `pbix_diff`,
`report_edits`, `report_revisions`). Both are small (well under 1 MiB), deterministic, and hold no real
data. Tests that need the user's real dashboard (`Downloads/Agents Performance - Dashboard.pbix`) must
`pytest.skip` when it is absent; nothing derived from it beyond these fixtures is committed.

| File | Size | What it is |
|---|---|---|
| `structure-twin.pbix` | 90 KB | Scrubbed copy of the structure of the real Agents Performance report (1 page, 15 visuals). |
| `multipage.pbix` | 100 KB | The twin grown into 4 pages with bookmarks, groups, drillthrough, tooltip, theme and filters. |

## structure-twin.pbix

Made by `scripts/make_structure_twin.py` from the real file, which cannot be committed because its
DataModel holds employee names.

Kept exactly as Power BI Desktop 2.157 wrote it:
- the zip member order, compression types and timestamps;
- the encodings: UTF-16LE without a BOM for `Version`, `DiagramLayout`, `Settings`, `Metadata` and
  `DAXQueries/.pbi/daxQueries.json`; UTF-8 with a BOM for `[Content_Types].xml`; compact UTF-8 for PBIR JSON;
- every `Report/definition` part token for token (number spellings such as `966.14173228346453` survive).
  Every string literal of the real report was reviewed on 2026-09-28 (business captions, helper-table
  category values, formatting tokens); a literal that is not on the reviewed list is replaced with
  `Text N`. On the current report nothing needed replacing.

Replaced:
- `DataModel`: 64 KiB of deterministic filler, STORED. Studio streams it and never parses it, so tests must
  not expect a readable model (pbixray cannot open the twin).
- `SecurityBindings`: filler of the original length. Studio drops it on every write.
- Images under `Report/StaticResources`: a 1x1 PNG with the same name.
- `DAXQueries/Query%201.dax`: a generic query in the original encoding.

Before writing, the script reads the source model with pbixray and refuses to write if any value of a name,
e-mail or phone column appears as a word in the twin's text parts. It also checks member order, compression,
encodings and JSON structure against the source.

Deliberately lacks: real data, bookmarks, groups, drillthrough or tooltip pages, custom themes, mobile layouts.

Regenerate (needs the real file, and `pbixray` from the project venv):

    .venv\Scripts\python.exe scripts\make_structure_twin.py

The output is byte-for-byte reproducible; `tests/unit/test_imported_fixtures.py` regenerates it and compares.

## multipage.pbix

Made by `scripts/make_multipage_fixture.py` from `structure-twin.pbix` (it needs nothing else and no
network). Everything the script does not touch is the twin's bytes, so the 15 page-1 visuals, the model
filler, `DiagramLayout` and the encodings are still Desktop-authored.

Pages, in order:

| Page id | Display name | Notes |
|---|---|---|
| `ReportSection8454c812b60b8bb83b99` | `1` | The twin's page, plus the additions below. Active page. |
| `ReportSection3f1c0d5e7a9b2c4d6e8f` | `Details` | Nested groups, image visual, bookmark button. |
| `ReportSectiond2a4c6e8f0b1a3c5e7d9` | `Store drillthrough` | `pageBinding` type `Drillthrough` with a `boundFilter`. |
| `ReportSectionb7e9f1a3c5d7e9f1a3b5` | `Sales tooltip` | The only hidden page. `pageBinding` type `Tooltip`, 320x240. |

What was added to the twin, and where it came from:

| Construct | Where | Provenance |
|---|---|---|
| Custom theme `FixtureTheme.json` (12 data colors, `textClasses`, `visualStyles`), registered as `CustomTheme` in `RegisteredResources`, `themeCollection.customTheme` | `report.json`, `RegisteredResources/` | Hand-built. Shape follows what Desktop 2.157 writes for `SetTheme` (the twin's own `baseTheme` entry and `resourcePackages` items). |
| Image resource `FixtureLogo.png` and an `image` visual using `ResourcePackageItem` | `Details` page | Hand-built. The `ResourcePackageItem` expression is copied from the twin's page-background image. |
| Report filter, page filter (categorical, `From` alias, `Source` in `Where`) | `report.json`, page 1 `page.json` | Encoded as the twin's Desktop-written table filter is (`Version` 2, `From`, `Source`, `howCreated`). Hand-built values. |
| TopN visual filter with a `Subquery` | Table visual `e82e02f9...` (after its existing Desktop-written Advanced filter) | Hand-built from the Desktop encoding of a Top N filter. |
| Conditional-format `fontColor` as a `Measure` expression | Table visual `e82e02f9...` | Desktop-authored, from the twin. |
| `visualInteractions` (`NoFilter`, `DataFilter`) | Page 1 `page.json` | Hand-built. |
| `visualTooltip` (`type` `ReportPage`, `section` = tooltip page) | Column chart `b404a461...` | Hand-built. |
| Action button with `visualLink` `PageNavigation` to `Details`; another with `visualLink` `Bookmark` | Page 1, `Details` | Hand-built. The button copies a Desktop slicer's frame and replaces its `visual` body. |
| Two bookmarks and `bookmarks/bookmarks.json` | `Report/definition/bookmarks/` | Hand-built from the bookmark schemas: `explorationState` with `activeSection`, per-visual `singleVisual`, `options.targetVisualNames`. |
| Group `Header block` containing group `Filters`, with children whose `parentGroupName` names their group | `Details` page | Hand-built (`visualGroup` with `displayName` and `groupMode`). The twin has no groups. |
| Drillthrough and tooltip pages | Last two pages | Hand-built. |
| `mobile.json` for the page-1 navigation button | `visuals/eff2d7b0.../` | Hand-built, position only. |

Not verified against Desktop: bookmarks, groups, drillthrough and tooltip pages, the `page.json` `type`
property, `mobile.json` and the TopN subquery were written from the published PBIR schemas and remembered
Desktop output, not saved by Desktop. The `$schema` URLs for `visualContainer`, `page`, `report` and
`pagesMetadata` are the twin's own. Those for `bookmark` (2.1.0), `bookmarksMetadata` (1.0.0) and
`visualContainerMobileState` (2.6.0) are not in any Desktop file we have, so their versions are unverified
(they are constants in the script). A Desktop open-and-save of this file, checked with
`pbix_diff.survival`, is the way to confirm the constructs; until then treat them as plausible, not
authoritative. Names are deterministic (`sha256` of a label), not random.

Deliberately lacks: real data (the model is filler, the filter values are `Store 1`, `Store 2`, `Online`),
a thin-report variant (`reportExtensions.json` is tested with a package built in `tmp_path`), and a
`SharedResources` custom theme. The variant whose custom theme lives in `SharedResources` (a gallery theme
Desktop copied in) is built in a test by moving the theme part, not committed.

Regenerate:

    .venv\Scripts\python.exe scripts\make_multipage_fixture.py

It writes `fixtures/imported/multipage.pbix`, refuses if a reference it wrote does not resolve, and gives the
same bytes every time.

## Tests

`tests/unit/test_imported_fixtures.py` checks size, privacy (no user name, path or e-mail in any part),
byte-for-byte regeneration, every construct above, and that both fixtures round-trip through `PbixPackage`
with an empty `semantic_diff`. Builders for small synthetic PBIX files are in `tests/unit/pbix_factory.py`.
