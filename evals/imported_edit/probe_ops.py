r"""Try every operation family's Desktop probes in real Power BI Desktop.

    .venv\Scripts\python.exe evals\imported_edit\probe_ops.py --pbix "C:\path\Agents Performance - Dashboard.pbix"
        [--only name,name] [--family conditional] [--work DIR]

Each ``ProbeCase`` (``edit_tools.desktop_probes()``) is applied to its own scratch copy of the
dashboard through the same operation layer chat and the Visual pane use, validated, and written.
The copy is opened in Power BI Desktop, every page is screenshotted through the bridge, Desktop's own
change is saved (UI Automation), and the saved file is compared with what Studio wrote
(``pbix_diff.survival``): anything Desktop dropped or rewrote is a failure. ``ProbeCase.check`` then
inspects the saved JSON. Desktop is closed the safe way only; one that cannot be closed is left open and
listed. The source file is only read.

The receipt (``evals/imported_edit/evidence/probes-<stamp>.json``) lists per case: applied, the
operation summaries, validation problems, survival problems, check problems, screenshots and seconds.
Screenshots stay in the work folder (they show the dashboard's data, so they are not committed).
"""

# ruff: noqa: E501 - log and receipt lines are long human-readable messages
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from ai_powerbi_analyst.studio import pbix_diff, pbix_extract  # noqa: E402
from ai_powerbi_analyst.studio import report_edits as edits  # noqa: E402
from ai_powerbi_analyst.studio.desktop_session import (  # noqa: E402
    DesktopController,
    DesktopSessionError,
)
from ai_powerbi_analyst.studio.edit_tools import ProbeCase, desktop_probes  # noqa: E402
from ai_powerbi_analyst.studio.pbir_document import PbirDocument  # noqa: E402
from ai_powerbi_analyst.studio.pbix_package import PbixPackage, decode_json  # noqa: E402

NODE = Path.home() / "AppData" / "Local" / "hermes" / "node"
BRIDGE = [str(NODE / "node.exe"), str(NODE / "node_modules/@microsoft/powerbi-desktop-bridge-cli/dist/index.js")]
PBI = str(Path.home() / ".local" / "bin" / "pbi.exe")


def desktop_exe() -> str:
    """The installed PBIDesktop.exe, found the way Studio finds it (the WindowsApps folder cannot
    be listed, so no globbing)."""
    from ai_powerbi_analyst.preflight import probe_powerbi_desktop

    found = probe_powerbi_desktop()
    if not found.available or not found.path:
        raise SystemExit("Power BI Desktop is not installed")
    return str(found.path)


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def model_info(source: Path) -> edits.ModelInfo:
    facts = pbix_extract.types(source)
    columns = [
        {"TableName": c["table"], "ColumnName": c["name"], "PandasDataType": str(c.get("dataType", ""))}
        for c in facts.get("columns", [])
    ]
    measures = pbix_extract.measures(source)
    return edits.ModelInfo.from_report(columns, measures, facts, calculated=facts.get("calculated"))


def saved_parts(path: Path) -> dict[str, Any]:
    package = PbixPackage(path)
    return {
        name: decode_json(package.read(name))
        for name in package.names()
        if name.startswith("Report/") and name.endswith(".json")
    }


# Families not yet registered in the facade parse and apply through their own dispatchers
# (module, parse, apply); once registered, the facade parses every step and these go unused.
_FAMILY_DISPATCH = (
    ("report_edit_conditional", "parse_conditional", "apply_conditional"),
    ("report_edit_navigation", "parse_navigation", "apply_navigation"),
    ("report_edit_extras", "parse_extras", "apply_extras"),
)


def _dispatchers() -> list[tuple[Any, Any]]:
    import importlib

    found = []
    for module_name, parse_name, apply_name in _FAMILY_DISPATCH:
        try:
            module = importlib.import_module(f"ai_powerbi_analyst.studio.{module_name}")
        except ImportError:
            continue
        found.append((getattr(module, parse_name), getattr(module, apply_name)))
    return found


def _parse_steps(raw: list[dict[str, Any]]) -> list[tuple[Any, Any]]:
    """Each step with the apply function that owns it (None = the facade)."""
    families = _dispatchers()
    steps: list[tuple[Any, Any]] = []
    for item in raw:
        try:
            steps.append((None, edits.parse_operations([item])[0]))
            continue
        except Exception as facade_error:  # noqa: BLE001 - try the unregistered families
            error: Exception = facade_error
        for parse, apply in families:
            try:
                steps.append((apply, parse([item])[0]))
                break
            except Exception as family_error:  # noqa: BLE001 - the next family may own it
                error = family_error
        else:
            raise error
    return steps


def apply_case(case: ProbeCase, source: Path, target: Path, model: edits.ModelInfo) -> dict[str, Any]:
    package = PbixPackage(source)  # read the source, write the case's own copy
    document = PbirDocument(package)
    steps = _parse_steps([dict(op) for op in case.operations])
    summaries: list[str] = []
    warnings: list[str] = []
    model_operations: list[str] = []
    index = 0
    while index < len(steps):  # consecutive steps of one owner apply together, in order
        owner = steps[index][0]
        run = []
        while index < len(steps) and steps[index][0] is owner:
            run.append(steps[index][1])
            index += 1
        outcome = (owner or edits.apply_operations)(document, run, model)
        summaries.extend(outcome.summaries)
        warnings.extend(outcome.warnings)
        model_operations.extend(op.op for op in outcome.model_operations)
    problems = edits.validate_document(document)
    document.commit()
    package.write(target)
    return {
        "summaries": summaries,
        "warnings": warnings,
        "model_operations": model_operations,
        "validation": problems,
        "pages": [(page.name, page.display_name) for page in document.pages.values()],
    }


def run_case(controller: DesktopController, case: ProbeCase, source: Path, work: Path, model: edits.ModelInfo, index: int) -> dict[str, Any]:
    started = time.monotonic()
    result: dict[str, Any] = {"name": case.name, "family": case.family, "expect": case.expect}
    written = work / f"{index:02d}-{case.name}.pbix"
    try:
        result.update(apply_case(case, source, written, model))
    except Exception as error:  # noqa: BLE001 - a case that cannot even be written is a finding
        result.update(ok=False, stage="apply", error=f"{type(error).__name__}: {error}", seconds=round(time.monotonic() - started, 1))
        return result
    if result["model_operations"]:
        result.update(ok=False, stage="apply", error="probe cases are report-layer only")
        return result
    kept = work / f"{index:02d}-{case.name}.written.pbix"
    shutil.copyfile(written, kept)
    instance = None
    try:
        instance = controller.open(written, normalize=False)
        time.sleep(8)
        shots = []
        for page_id, display in result["pages"]:
            shot = work / f"{index:02d}-{case.name}-{page_id[-8:]}.png"
            try:
                controller.screenshot(instance, page_id, shot)
                shots.append({"page": display, "file": str(shot)})
            except DesktopSessionError as error:
                shots.append({"page": display, "error": str(error)})
        result["screenshots"] = shots
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not controller.status(instance).get("hasUnsavedChanges"):
            time.sleep(1)
        controller.save(instance)
        controller.close(instance)
        instance = None
        result["survival"] = pbix_diff.survival(kept, written, baseline=source)
        result["check"] = case.check(saved_parts(written)) if case.check else []
        result["ok"] = not result["validation"] and not result["survival"] and not result["check"]
    except Exception as error:  # noqa: BLE001 - recorded; the next case still runs
        result.update(ok=False, stage="desktop", error=f"{type(error).__name__}: {error}", trace=traceback.format_exc(limit=3)[-600:])
        if instance is not None:
            result["left_open"] = {"pid": instance.pid, "file": str(written)}
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--pbix", required=True, type=Path)
    parser.add_argument("--only", default="")
    parser.add_argument("--family", default="")
    parser.add_argument("--work", type=Path)
    parser.add_argument("--desktop-exe", default="")
    args = parser.parse_args()
    cases = list(desktop_probes())
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        cases = [c for c in cases if c.name in wanted]
    if args.family:
        cases = [c for c in cases if c.family == args.family]
    if not cases:
        log("no probe cases")
        return 1
    work = args.work or Path(tempfile.mkdtemp(prefix="studio-probes-"))
    work.mkdir(parents=True, exist_ok=True)
    source = work / "source.pbix"
    shutil.copyfile(args.pbix, source)
    desktop = args.desktop_exe or desktop_exe()
    controller = DesktopController(BRIDGE, PBI, desktop)
    model = model_info(source)
    results = []
    for index, case in enumerate(cases, 1):
        log(f"{index}/{len(cases)} {case.family}: {case.name}")
        result = run_case(controller, case, source, work, model, index)
        results.append(result)
        log(f"  -> {'ok' if result.get('ok') else 'FAILED'} ({result.get('seconds')} s) {result.get('error') or ''}"
            + (f" survival: {result['survival'][:2]}" if result.get("survival") else "")
            + (f" check: {result['check'][:2]}" if result.get("check") else "")
            + (f" validation: {result['validation'][:2]}" if result.get("validation") else ""))
        if result.get("left_open"):
            log("  a Desktop window was left open; stopping so nothing piles up")
            break
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = REPO / "evals" / "imported_edit" / "evidence" / f"probes-{stamp}.json"
    out.write_text(json.dumps({"desktop": desktop, "work": str(work), "cases": results}, indent=1, default=str), encoding="utf-8")
    failed = [r["name"] for r in results if not r.get("ok")]
    log(f"{len(results) - len(failed)} of {len(results)} ok; receipt {out}")
    for name in failed:
        log(f"  FAILED {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
