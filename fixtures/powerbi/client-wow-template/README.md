# Client-Wow Power BI preparation template

**NOT DESKTOP-VERIFIED:** this is an official-shape PBIP/PBIR `byPath` preparation
template created on macOS. It must not be presented as a Power BI Desktop-openable
artifact until the explicit Windows gate below succeeds.

The TMDL model exposes every table, dimension, metric, and prepared report object named
by `fixtures/data/semantic_catalog.yaml`. This includes disconnected zero-row
preparation tables for Driver Contributions, Forecast, and Scenario Grid; their
measures are valid bindings, but they intentionally return blank until the governed
preparation command hydrates their frozen Parquet sources. The report's one preparation page deliberately uses
the stable Executive Pulse page ID, so the first approved recipe application replaces
it instead of leaving a fourth demo page. On the target Windows laptop:

The two pinned report validators check PBIR report structure; they do not prove that
the placeholder TMDL loads, calculates, or renders in Desktop. Those remain explicit
Windows preparation gates.

1. From the repository root, run `scripts\prepare-windows-demo.ps1`. It copies this
   template without overwriting existing state and hydrates all governed partitions.
2. Confirm `definition.pbir` remains a `byPath` reference and the prepared fixture
   manifest matches the frozen catalog and data hashes.
3. With Desktop closed, run `pbi --json report --path ClientWow.Report validate --full`.
4. In strict offline mode, run `powerbi-report-author validate ClientWow.Report` with
   schemas already cached/frozen; any warning blocks release.
5. Use `Phase0KillTestRunner` for at least ten mutate → validate → exact-undo cycles.
6. Supply its explicit Desktop confirmation hook for each reopen/render check. Without
   that hook, `phase_gate_passed` deliberately remains false.
7. Record the evidence, then run `scripts\attest-windows-demo.ps1`; preparation by
   itself never enables real writes.

Do not change `catalog-bindings.json` independently of the governed catalog.
