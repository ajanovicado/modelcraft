from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from ai_powerbi_analyst.powerbi import desktop_hydration
from ai_powerbi_analyst.powerbi.desktop_hydration import (
    DesktopHydrationError,
    DesktopHydrationVerifier,
    _run_command,
)


def test_cli_json_uses_utf8_for_unicode_screenshot_paths() -> None:
    result = _run_command(
        (
            sys.executable,
            "-c",
            'import sys; sys.stdout.buffer.write(b\'{"path":"Sales \\xc2\\xb7 LOD.png"}\')',
        ),
        10,
        None,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["path"] == "Sales · LOD.png"


@dataclass(frozen=True)
class _Result:
    returncode: int
    stdout: str
    stderr: str = ""


class _Process:
    def __init__(self, pid: int, name: str, engine_directory: Path | None = None):
        self.pid = pid
        self.process_name = name
        self.engine_directory = engine_directory
        self.alive = True
        self.child_processes: list[_Process] = []

    def children(self):
        return self.child_processes

    def name(self):
        return self.process_name

    def is_running(self):
        return self.alive

    def cmdline(self):
        return [self.process_name, "-s", str(self.engine_directory)]


@pytest.fixture(autouse=True)
def desktop_processes(tmp_path, monkeypatch):
    engine_directory = tmp_path / "target-engine"
    engine_directory.mkdir()
    (engine_directory / "msmdsrv.port.txt").write_text("12345", encoding="utf-16-le")
    model = _Process(5432, "msmdsrv.exe", engine_directory)
    desktop = _Process(4321, "PBIDesktop.exe")
    desktop.child_processes = [model]
    unrelated = _Process(8765, "PBIDesktop.exe")
    processes = {4321: desktop, 5432: model, 8765: unrelated}

    def process(pid):
        if pid not in processes:
            raise desktop_hydration.psutil.NoSuchProcess(pid)
        return processes[pid]

    monkeypatch.setattr(desktop_hydration.psutil, "Process", process)
    return processes


def _write_pbip(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Sales"
    report = project / "Sales.Report"
    model = project / "Sales.SemanticModel"
    (report / "definition/pages/overview/visuals/chart").mkdir(parents=True)
    (report / "definition/pages/details/visuals/text").mkdir(parents=True)
    (model / "definition").mkdir(parents=True)
    pbip = project / "Sales.pbip"
    pbip.write_text(
        json.dumps({"artifacts": [{"report": {"path": "Sales.Report"}}]}),
        encoding="utf-8",
    )
    (report / "definition.pbir").write_text(
        json.dumps(
            {
                "$schema": "report-schema",
                "datasetReference": {
                    "byPath": {"path": "../Sales.SemanticModel"},
                },
            }
        ),
        encoding="utf-8",
    )
    (report / "definition/pages/pages.json").write_text(
        json.dumps({"pageOrder": ["overview", "details"]}),
        encoding="utf-8",
    )
    for page in ("overview", "details"):
        (report / "definition/pages" / page / "page.json").write_text(
            json.dumps({"name": page}),
            encoding="utf-8",
        )
    (report / "definition/pages/overview/visuals/chart/visual.json").write_text(
        json.dumps(
            {
                "visual": {
                    "query": {
                        "field": {
                            "Aggregation": {"Expression": {"SourceRef": {"Entity": "Orders"}}}
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (report / "definition/pages/details/visuals/text/visual.json").write_text(
        json.dumps({"visual": {"visualType": "textbox"}}),
        encoding="utf-8",
    )
    (model / "definition.pbism").write_text(
        json.dumps({"$schema": "model-schema", "version": "4.0"}),
        encoding="utf-8",
    )
    (model / "definition/model.tmdl").write_text("model Model\n", encoding="utf-8")
    return pbip, report / "definition.pbir"


class _Runner:
    def __init__(
        self,
        pbip: Path,
        *,
        mutate_path: Path | None = None,
        row_count: str = "9994",
        transient_status: bool = False,
    ):
        self.pbip = pbip
        self.mutate_path = mutate_path
        self.row_count = row_count
        self.transient_status = transient_status
        self.status_calls = 0
        self.calls: list[tuple[str, ...]] = []
        self.environments: list[Mapping[str, str] | None] = []
        self.connection: str | None = None

    def __call__(
        self,
        argv: tuple[str, ...],
        _timeout: float,
        environment: Mapping[str, str] | None,
    ) -> _Result:
        self.calls.append(argv)
        self.environments.append(environment)
        command = Path(argv[0]).name.casefold()
        if command == "bridge" and argv[1] == "open":
            return _Result(
                0,
                "Launching Power BI Desktop...\n"
                + json.dumps(
                    {
                        "status": "launched",
                        "bridgeStatus": "connected",
                        "pid": 4321,
                        "launched": {"reportPath": str(self.pbip)},
                    }
                ),
            )
        if command == "bridge" and argv[1] == "status":
            self.status_calls += 1
            if self.transient_status and self.status_calls == 1:
                return _Result(0, json.dumps({"status": "not_connected", "instances": []}))
            return _Result(
                0,
                json.dumps(
                    {
                        "status": "ready",
                        "instances": [
                            {
                                "pid": 8765,
                                "bridgeStatus": "connected",
                                "currentFilePath": str(self.pbip.parent / "Unrelated.pbip"),
                            },
                            {
                                "pid": 4321,
                                "bridgeStatus": "connected",
                                "currentFilePath": str(self.pbip),
                            },
                        ],
                    }
                ),
            )
        if command == "pbi" and argv[1:3] == ("--json", "connect"):
            assert argv[3:6] == ("--data-source", "localhost:12345", "--name")
            self.connection = argv[6]
            assert self.connection.startswith("studio-hydrate-4321-")
            return _Result(
                0,
                json.dumps({"status": "connected", "connection": self.connection}),
            )
        if command == "pbi":
            assert self.connection is not None
            assert argv[argv.index("--connection") + 1] == self.connection
        if command == "pbi" and argv[-2:] == ("table", "list"):
            return _Result(0, json.dumps([{"name": "Orders"}, {"name": "People"}]))
        if command == "pbi" and "refresh" in argv:
            table = argv[argv.index("refresh") + 1]
            return _Result(0, json.dumps({"status": "refreshed", "name": table}))
        if command == "pbi" and "dax" in argv:
            return _Result(
                0,
                json.dumps(
                    {
                        "columns": ["__row_count"],
                        "rows": [{"[__row_count]": self.row_count}],
                    }
                ),
            )
        if command == "bridge" and argv[1] == "screenshot-all":
            output = Path(argv[argv.index("--output-dir") + 1])
            screenshots = []
            for page in ("overview", "details"):
                path = output / f"{page}.png"
                path.write_bytes(b"png")
                screenshots.append({"pageId": page, "outputPath": str(path)})
            if self.mutate_path is not None:
                self.mutate_path.write_text('{"desktop":"changed"}', encoding="utf-8")
            payload = {
                "status": "ok",
                "screenshots": screenshots,
                "failures": [],
            }
            return _Result(
                0,
                "Capturing page overview...\nCapturing page details...\n" + json.dumps(payload),
            )
        raise AssertionError(f"Unexpected command: {argv}")


def test_hydrates_all_tables_probes_visual_data_and_captures_every_page(
    tmp_path: Path,
) -> None:
    pbip, definition = _write_pbip(tmp_path)
    original_definition = definition.read_bytes()
    runner = _Runner(pbip, transient_status=True)
    sleeps: list[float] = []
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable=desktop,
        run_command=runner,
        sleep=sleeps.append,
    )

    baseline = verifier.capture_baseline(pbip)
    verifier.open_with_bridge(pbip)
    result = verifier.hydrate_and_verify(baseline, tmp_path / "evidence")

    assert result.data_refreshed
    assert result.render_verified
    assert result.desktop_pid == 4321
    assert not result.cache_saved
    assert result.refreshed_tables == ("Orders", "People")
    assert result.referenced_table_rows == (("Orders", 9994),)
    assert [Path(path).name for path in result.screenshot_paths] == [
        "overview.png",
        "details.png",
    ]
    assert definition.read_bytes() == original_definition
    receipt = json.loads((Path(result.evidence_path) / "render-verification.json").read_text())
    assert receipt["render_verified"] is True
    screenshot_call = next(argv for argv in runner.calls if "screenshot-all" in argv)
    assert screenshot_call[screenshot_call.index("--settle") + 1] == "5000"
    assert any("--max-rows" in argv and "1" in argv for argv in runner.calls)
    open_index = next(index for index, argv in enumerate(runner.calls) if "open" in argv)
    assert runner.environments[open_index] is not None
    assert runner.environments[open_index]["PBI_DESKTOP_PATH"] == str(desktop.resolve())
    assert runner.status_calls == 2
    assert sleeps == [1.0]
    status_calls = [argv for argv in runner.calls if len(argv) > 1 and argv[1] == "status"]
    assert all(argv[argv.index("--wait-seconds") + 1] == "4" for argv in status_calls)
    assert all(
        environment is None
        for index, environment in enumerate(runner.environments)
        if index != open_index
    )


def test_fails_closed_if_desktop_changes_committed_source_bytes(tmp_path: Path) -> None:
    pbip, definition = _write_pbip(tmp_path)
    runner = _Runner(pbip, mutate_path=definition)
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable=desktop,
        run_command=runner,
    )
    baseline = verifier.capture_baseline(pbip)

    with pytest.raises(DesktopHydrationError, match="changed committed PBIP source"):
        verifier.hydrate_and_verify(baseline, tmp_path / "evidence")

    receipt = next((tmp_path / "evidence").glob("*/render-verification.json"))
    assert json.loads(receipt.read_text())["render_verified"] is False


@pytest.mark.parametrize("row_count", ["0", "01", "-1", "1.5"])
def test_rejects_empty_or_noncanonical_visual_table_row_counts(
    tmp_path: Path,
    row_count: str,
) -> None:
    pbip, _definition = _write_pbip(tmp_path)
    runner = _Runner(pbip, row_count=row_count)
    desktop = tmp_path / "PBIDesktop.exe"
    desktop.write_bytes(b"desktop")
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable=desktop,
        run_command=runner,
    )
    baseline = verifier.capture_baseline(pbip)

    with pytest.raises(DesktopHydrationError):
        verifier.hydrate_and_verify(baseline, tmp_path / "evidence")

    assert not any("screenshot-all" in argv for argv in runner.calls)


@pytest.mark.parametrize("expired_pid", [4321, 5432])
def test_expired_target_process_never_connects_or_refreshes_another_report(
    tmp_path, desktop_processes, expired_pid
):
    pbip, definition = _write_pbip(tmp_path)
    original = definition.read_bytes()
    runner = _Runner(pbip)
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=runner,
    )
    baseline = verifier.capture_baseline(pbip)
    desktop_processes[expired_pid].alive = False
    assert desktop_processes[8765].alive  # An unrelated Desktop remains open.

    with pytest.raises(DesktopHydrationError, match="calculation engine closed"):
        verifier.hydrate_and_verify(baseline, tmp_path / "evidence")

    assert not any(Path(argv[0]).name == "pbi" for argv in runner.calls)
    assert definition.read_bytes() == original
    receipt = next((tmp_path / "evidence").glob("*/render-verification.json"))
    assert json.loads(receipt.read_text())["render_verified"] is False


@pytest.mark.parametrize("engine_count", [0, 2])
def test_missing_or_ambiguous_target_engine_has_no_default_connection_fallback(
    tmp_path, desktop_processes, engine_count
):
    runner = _Runner(tmp_path / "unused.pbip")
    desktop_processes[4321].child_processes = [desktop_processes[5432]] * engine_count
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=runner,
    )
    with pytest.raises(DesktopHydrationError, match="unavailable or ambiguous"):
        verifier._connect(4321)
    assert runner.calls == []


def test_vanished_verified_desktop_pid_never_uses_an_unrelated_live_process(
    tmp_path, desktop_processes
):
    runner = _Runner(tmp_path / "unused.pbip")
    del desktop_processes[4321]
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=runner,
    )
    with pytest.raises(DesktopHydrationError, match="calculation engine is unavailable"):
        verifier._connect(4321)
    assert runner.calls == []


@pytest.mark.parametrize("port", ["0", "65536", "१२३४५", "1" * 65])
def test_invalid_target_engine_port_never_launches_a_connection(tmp_path, desktop_processes, port):
    model = desktop_processes[5432]
    (model.engine_directory / "msmdsrv.port.txt").write_text(port, encoding="utf-16-le")
    runner = _Runner(tmp_path / "unused.pbip")
    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=runner,
    )
    with pytest.raises(DesktopHydrationError, match="port.*invalid"):
        verifier._connect(4321)
    assert runner.calls == []


def test_connection_receipt_for_a_different_model_never_refreshes(tmp_path):
    pbip, _ = _write_pbip(tmp_path)
    runner = _Runner(pbip)

    def wrong_connection(argv, timeout, environment):
        result = runner(argv, timeout, environment)
        if "connect" in argv:
            return _Result(0, json.dumps({"status": "connected", "connection": "other-report"}))
        return result

    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=wrong_connection,
    )
    with pytest.raises(DesktopHydrationError, match="did not confirm"):
        verifier.hydrate_and_verify(verifier.capture_baseline(pbip), tmp_path / "evidence")
    assert not any("refresh" in argv for argv in runner.calls)


@pytest.mark.parametrize("close_after", ["connect", "refresh"])
def test_target_model_exit_stops_remaining_refreshes(tmp_path, desktop_processes, close_after):
    pbip, _ = _write_pbip(tmp_path)
    runner = _Runner(pbip)

    def closing_model(argv, timeout, environment):
        result = runner(argv, timeout, environment)
        if close_after in argv:
            desktop_processes[5432].alive = False
        return result

    verifier = DesktopHydrationVerifier(
        pbi_executable="pbi",
        bridge_executable="bridge",
        desktop_executable="PBIDesktop.exe",
        run_command=closing_model,
    )
    with pytest.raises(DesktopHydrationError, match="calculation engine closed"):
        verifier.hydrate_and_verify(verifier.capture_baseline(pbip), tmp_path / "evidence")
    refreshes = [argv for argv in runner.calls if "refresh" in argv]
    assert len(refreshes) == (0 if close_after == "connect" else 1)
    assert not any("screenshot-all" in argv for argv in runner.calls)
