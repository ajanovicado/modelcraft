"""Real Desktop acceptance on disposable local data; never use a user's workspace.

Run with the repository virtualenv. Requires Power BI closed, pinned CLIs and Ollama.
Five input formats x (new build + reviewed rebuild) = ten Desktop cycles.
Stops at the first failed boundary, retaining all artifacts outside the repository.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import psutil
import pyarrow as pa
import pyarrow.parquet as pq
from openpyxl import Workbook

from ai_powerbi_analyst.powerbi.desktop_hydration import DesktopHydrationVerifier
from ai_powerbi_analyst.powerbi.transaction import hash_tree
from ai_powerbi_analyst.preflight import probe_pbi_cli, probe_powerbi_desktop, probe_tool
from ai_powerbi_analyst.studio.contracts import (
    ApproveDraftRequest,
    BuildRequest,
    CreateWorkspaceRequest,
    DraftRequest,
    ExportBuildRequest,
    IngestRequest,
    UndoBuildRequest,
    UpdateDashboardRequest,
)
from ai_powerbi_analyst.studio.service import StudioService
from ai_powerbi_analyst.studio.state import EMPTY_HASH


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, choices=range(1, 6), default=5)
    args = parser.parse_args()
    if any(p.info["name"].lower() == "pbidesktop.exe" for p in psutil.process_iter(["name"])):
        raise RuntimeError(
            "Close Power BI Desktop first; this harness never closes existing reports"
        )
    root = Path(tempfile.mkdtemp(prefix="studio-desktop-release-"))
    print(f"QA evidence: {root}", flush=True)
    repository = Path(__file__).resolve().parents[1]
    service = StudioService(repository, root / "state")
    # Isolate compiler/Desktop qualification from model quality qualification.
    service.planner.model = None
    report = {
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "scope": "new projects and managed rebuilds; not in-place migrations",
        "model_generation": "not tested in this harness",
        "cycles": [],
    }
    stop = Event()
    available = []

    def sample_memory():
        while not stop.is_set():
            available.append(psutil.virtual_memory().available / 1024**3)
            stop.wait(0.5)

    sampler = Thread(target=sample_memory, daemon=True)
    sampler.start()

    def save():
        report["minimum_available_memory_gib"] = min(available) if available else None
        (root / "acceptance.json").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )

    try:
        report["environment"] = service.health().model_dump(mode="json")
        for index, kind in enumerate(
            ["utf8", "cp1252", "utf16", "xlsx", "parquet"][: args.cases], 1
        ):
            source_root = root / f"input-{kind}"
            source_root.mkdir()
            data = {
                "Order ID": ["A1", "A2", "A3"],
                "Order Date": ["2025-01-01", "2025-02-01", "2025-03-01"],
                "Region": ["Québec", "West", "Québec"],
                "Sales": [100.5 * index, 250 * index, 149.5 * index],
                "Profit": [20.25 * index, 40 * index, 29.75 * index],
            }
            rows = list(zip(*data.values(), strict=True))
            source = source_root / ("orders." + (kind if kind in {"xlsx", "parquet"} else "csv"))
            if kind == "xlsx":
                book = Workbook()
                sheet = book.active
                sheet.title = "orders"
                sheet.append(list(data))
                for row in rows:
                    sheet.append(row)
                book.save(source)
                book.close()
            elif kind == "parquet":
                pq.write_table(pa.table(data), source)
            else:
                text = (
                    ",".join(data)
                    + "\n"
                    + "\n".join(",".join(map(str, row)) for row in rows)
                    + "\n"
                )
                source.write_text(
                    text, encoding={"utf8": "utf-8", "cp1252": "cp1252", "utf16": "utf-16"}[kind]
                )
            workspace = service.create_workspace(
                CreateWorkspaceRequest(
                    name=f"Release {kind}",
                    selected_folder=str(source_root),
                    idempotency_key=f"workspace-{kind}",
                    expected_base_hash=EMPTY_HASH,
                )
            )
            snapshot = service.ingest(
                IngestRequest(
                    workspace_id=workspace.id,
                    item_paths=[str(source)],
                    idempotency_key=f"ingest-{kind}",
                    expected_base_hash=workspace.base_hash,
                )
            )
            workspace = service.workspace(workspace.id)
            draft = service.create_draft(
                DraftRequest(
                    workspace_id=workspace.id,
                    snapshot_id=snapshot.id,
                    prompt="Sales, profit and order count by region and date",
                    idempotency_key=f"draft-{kind}",
                    expected_base_hash=workspace.base_hash,
                )
            )
            first_hash = None
            first_build = None
            for revision in range(2):
                if revision:
                    first = draft.pages[0].model_copy(update={"title": "Overview reviewed"})
                    draft = service.update_dashboard(
                        draft.id,
                        UpdateDashboardRequest(
                            workspace_id=workspace.id,
                            pages=[first, *draft.pages[1:]],
                            idempotency_key=f"edit-{kind}",
                            expected_base_hash=draft.base_hash,
                        ),
                    )
                draft = service.approve_draft(
                    draft.id,
                    ApproveDraftRequest(
                        workspace_id=workspace.id,
                        approved_relationship_ids=[],
                        idempotency_key=f"approve-{kind}-{revision}",
                        expected_base_hash=draft.base_hash,
                    ),
                )
                workspace = service.workspace(workspace.id)
                started = time.perf_counter()
                job = service.build(
                    BuildRequest(
                        workspace_id=workspace.id,
                        draft_id=draft.id,
                        project_name=workspace.name,
                        idempotency_key=f"build-{kind}-{revision}",
                        expected_base_hash=workspace.base_hash,
                    )
                )
                cycle = {"format": kind, "revision": revision, "build": job.model_dump(mode="json")}
                report["cycles"].append(cycle)
                save()
                assert job.state.value == "committed", job.message
                if not revision:
                    first_hash, first_build = job.project_hash, job
                else:
                    assert job.project_hash != first_hash, (
                        "Reviewed title change did not reach PBIR"
                    )
                opened = service.open_powerbi(job.id)
                cycle["desktop"] = opened
                save()
                assert opened.get("opened") and opened.get("render_verified"), opened
                pid = int(opened["desktop_pid"])
                try:
                    verifier = DesktopHydrationVerifier(
                        pbi_executable=probe_pbi_cli().path,
                        bridge_executable=probe_tool("powerbi-desktop").path,
                        desktop_executable=probe_powerbi_desktop().path,
                    )
                    connection, desktop_process, model_process = verifier._connect(pid)
                    verifier._require_processes(desktop_process, model_process)
                    result = verifier._json_command(
                        (
                            verifier.pbi_executable,
                            "--json",
                            "--connection",
                            connection,
                            "dax",
                            "execute",
                            "--max-rows",
                            "1",
                            "--timeout",
                            "60",
                            'EVALUATE ROW("Sales", [Total Sales], "Profit", [Total Profit], "Orders", [Distinct Order ID])',
                        ),
                        "Release numerical golden query failed",
                    )
                    verifier._require_processes(desktop_process, model_process)
                    cycle["numerical_result"] = result
                    values = result["rows"][0]
                    normalized = {key.strip("[]"): float(value) for key, value in values.items()}
                    assert normalized == {
                        "Sales": 500 * index,
                        "Profit": 90 * index,
                        "Orders": 3,
                    }, normalized
                    assert hash_tree(Path(job.project_path).parent) == job.project_hash
                finally:
                    # This PID was positively identified as THIS disposable report by the bridge.
                    process = psutil.Process(pid)
                    assert process.name().lower() == "pbidesktop.exe"
                    process.terminate()
                    process.wait(timeout=20)
                cycle["elapsed_seconds"] = round(time.perf_counter() - started, 2)
                cycle["passed"] = True
                save()
                print(
                    f"PASS {kind} {'rebuild' if revision else 'new'}: all pages + numerical goldens",
                    flush=True,
                )
            undo = service.undo_build(
                job.id,
                UndoBuildRequest(
                    workspace_id=workspace.id,
                    idempotency_key=f"undo-{kind}",
                    expected_base_hash=workspace.base_hash,
                ),
            )
            assert undo.restored and undo.restored_hash == first_hash
            export_root = root / f"exports-{kind}"
            export_root.mkdir()
            exported = service.export_build(
                first_build.id,
                ExportBuildRequest(
                    workspace_id=workspace.id,
                    destination_folder=str(export_root),
                    idempotency_key=f"export-{kind}",
                    expected_base_hash=workspace.base_hash,
                ),
            )
            assert exported.project_hash == first_hash
            cycle["undo_byte_exact"] = True
            cycle["export_byte_exact"] = True
            save()
        report["status"] = "passed"
        return 0
    except Exception as error:
        report["status"] = "failed"
        report["error"] = str(error)
        report["traceback"] = traceback.format_exc()
        print(f"STOPPED at failed boundary: {error}", flush=True)
        return 1
    finally:
        stop.set()
        sampler.join()
        report["finished_at"] = datetime.now(UTC).isoformat()
        save()


if __name__ == "__main__":
    raise SystemExit(main())
