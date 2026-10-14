"""DesktopController with a fake machine: no Desktop, no bridge, no PowerShell, no processes.

The fake world holds processes, the bridge's instance list, Desktop's windows and a clock that
only moves when the controller sleeps, so every wait is deterministic. What these tests cannot
prove (that Desktop really outlives its parent, that its prompt cannot be answered through UI
Automation) is the contract script's job, run by a human against real Desktop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import types
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import pytest

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.studio import desktop_session, desktop_uia
from ai_powerbi_analyst.studio.desktop_session import (
    CREATE_BREAKAWAY_FROM_JOB,
    CREATE_NO_WINDOW,
    DesktopController,
    DesktopDialogOpen,
    DesktopInstance,
    DesktopSessionError,
    SaveResult,
    UnsavedDesktopWork,
    launch_argv,
    launch_command_line,
    launch_flags,
)

PBIX_NAME = "PBIDesktop.exe"
EXE = (
    r"C:\Program Files\WindowsApps"
    r"\Microsoft.MicrosoftPowerBIDesktop_2.157.1354.0_x64__x\bin\PBIDesktop.exe"
)
MAIN = {
    "name": "Report",
    "className": "Window",
    "enabled": True,
    "buttons": [{"id": "save", "name": "Save", "enabled": True}],
}


@dataclass
class Proc:
    name: str
    create_time: float
    alive: bool = True


class FakeProcess:
    def __init__(self, world: World, pid: int) -> None:
        proc = world.procs.get(pid)
        if proc is None or not proc.alive:
            raise psutil.NoSuchProcess(pid)
        self._world, self.pid, self._proc = world, pid, proc

    def name(self) -> str:
        return self._proc.name

    def create_time(self) -> float:
        return self._proc.create_time

    def children(self) -> list[Any]:
        return []

    def wait(self, timeout: float | None = None) -> int | None:
        self._world.waits.append((self.pid, timeout))
        if self.pid in self._world.exit_on_wait:
            self._world.remove(self.pid)
            return 0
        if not self._proc.alive:
            return None
        raise psutil.TimeoutExpired(timeout or 0)

    def kill(self) -> None:
        raise AssertionError("Studio must never kill a process")

    def terminate(self) -> None:
        raise AssertionError("Studio must never terminate a process")


class World:
    """One fake machine."""

    def __init__(self) -> None:
        self.now = 0.0
        self.procs: dict[int, Proc] = {}
        self.entries: dict[int, dict[str, Any]] = {}
        self.on_sleep: list[Callable[[], None]] = []
        self.bridge_calls: list[tuple[str, ...]] = []
        self.popen_calls: list[dict[str, Any]] = []
        self.popen_errors: list[OSError | None] = []  # one per call; None means it works
        self.popen_returncode = 0
        self.on_launch: Callable[[str], None] | None = None
        self.on_bridge_open: Callable[[str], None] | None = None
        self.uia_calls: list[tuple[str, int, str]] = []
        self.windows: dict[int, list[dict[str, Any]]] = {}
        self.invoke_error: Exception | None = None
        self.on_invoke: Callable[[int, str], None] | None = None
        self.close_posts: list[int] = []
        self.post_close_result = 1
        self.on_post_close: Callable[[int], None] | None = None
        self.exit_on_wait: set[int] = set()
        self.waits: list[tuple[int, float | None]] = []
        self.next_pid = 5000

    # time
    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        for hook in list(self.on_sleep):
            hook()

    # processes and bridge entries
    def add(
        self,
        pid: int,
        path: Path | str | None,
        *,
        created: float = 1000.0,
        connected: bool = True,
        dirty: bool = False,
        name: str = PBIX_NAME,
    ) -> DesktopInstance:
        self.procs[pid] = Proc(name, created)
        self.entries[pid] = {
            "pid": pid,
            "bridgeStatus": "connected" if connected else "connecting",
            "currentFilePath": str(path) if path else None,
            "hasUnsavedChanges": dirty,
        }
        return DesktopInstance(pid, created, Path(path) if path else Path())

    def remove(self, pid: int) -> None:
        self.procs[pid].alive = False
        self.entries.pop(pid, None)

    def spawn(self, path: str, *, connect_after: float = 2.0, dirty: bool = False) -> int:
        """Start a Desktop on ``path`` that the bridge lists as connected after a while."""
        self.next_pid += 1
        pid = self.next_pid
        self.add(pid, path, created=2000.0 + pid, connected=False, dirty=dirty)
        ready = self.now + connect_after

        def hook() -> None:
            if self.now >= ready and pid in self.entries:
                self.entries[pid]["bridgeStatus"] = "connected"

        self.on_sleep.append(hook)
        return pid

    # patched functions
    def bridge(self, *args: str, timeout: int = 60) -> dict[str, Any]:
        self.bridge_calls.append(args)
        if args[0] == "status":
            return {"instances": [dict(e) for e in self.entries.values()]}
        if args[0] == "open":
            if self.on_bridge_open:
                self.on_bridge_open(args[1])
            return {"status": "ok", "pid": 1}  # the pid `open` prints is never trusted
        if args[0] == "manifest":
            return {"status": "ok", "manifest": {"methods": []}}
        if args[0] == "screenshot":
            output = Path(args[args.index("--output") + 1])
            output.write_bytes(b"x" * 200)
            return {"status": "ok"}
        raise AssertionError(f"unexpected bridge call {args}")

    def popen(self, argv: str | list[str], **kwargs: Any) -> Any:
        self.popen_calls.append({"argv": argv, **kwargs})
        error = self.popen_errors.pop(0) if self.popen_errors else None
        if error is not None:
            raise error
        if self.on_launch:
            # the command line ends with the quoted report path
            self.on_launch(argv.rsplit('"', 2)[-2] if isinstance(argv, str) else argv[-1])
        return types.SimpleNamespace(
            returncode=self.popen_returncode, wait=lambda timeout=None: self.popen_returncode
        )

    def uia_windows(self, pid: int) -> list[dict[str, Any]]:
        self.uia_calls.append(("windows", pid, ""))
        return self.windows.get(pid, [dict(MAIN)])

    def uia_invoke(self, pid: int, automation_id: str = "", name: str = "") -> dict[str, Any]:
        self.uia_calls.append(("invoke", pid, automation_id or name))
        if self.invoke_error is not None:
            raise self.invoke_error
        if self.on_invoke:
            self.on_invoke(pid, automation_id)
        return {"id": automation_id}

    def post_close(self, pid: int) -> int:
        self.close_posts.append(pid)
        if self.on_post_close:
            self.on_post_close(pid)
        return self.post_close_result


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()
    monkeypatch.setattr(psutil, "Process", lambda pid: FakeProcess(w, pid))

    def process_iter(attrs: list[str] | None = None) -> list[Any]:
        return [
            types.SimpleNamespace(pid=pid, info={"name": proc.name})
            for pid, proc in w.procs.items()
            if proc.alive
        ]

    monkeypatch.setattr(psutil, "process_iter", process_iter)
    shim = types.SimpleNamespace(
        Popen=w.popen,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
        run=subprocess.run,
    )
    monkeypatch.setattr(desktop_session, "subprocess", shim)
    monkeypatch.setattr(desktop_session.desktop_uia, "windows", w.uia_windows)
    monkeypatch.setattr(desktop_session.desktop_uia, "invoke", w.uia_invoke)
    monkeypatch.setattr(desktop_session, "_post_close", w.post_close)
    monkeypatch.setattr(desktop_session, "job_limit_flags", lambda: None)  # breakaway allowed
    return w


def controller(world: World, **kwargs: Any) -> DesktopController:
    c = DesktopController(
        ["node", "bridge.js"], "pbi", EXE, clock=world.monotonic, sleep=world.sleep, **kwargs
    )
    c._bridge = world.bridge  # type: ignore[method-assign]
    return c


def make_pbix(path: Path, tag: str = "a") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("Report/definition/report.json", json.dumps({"tag": tag * 50}))
        archive.writestr("DataModel", b"model-" + tag.encode() * 100)
    return path


# --- identity ---------------------------------------------------------------------------------


def test_instance_json_round_trip_and_rejects_garbage(tmp_path: Path) -> None:
    instance = DesktopInstance(4242, 1727500000.123, tmp_path / "Report.pbix")
    saved = instance.to_json()
    assert saved == {
        "pid": 4242,
        "create_time": 1727500000.123,
        "path": str(tmp_path / "Report.pbix"),
    }
    assert DesktopInstance.from_json(saved) == instance
    assert DesktopInstance.from_json(json.dumps(saved)) == instance
    for bad in (
        {},
        {"pid": True, "create_time": 1.0, "path": "x"},
        {"pid": 0, "create_time": 1.0, "path": "x"},
        {"pid": 1, "create_time": float("nan"), "path": "x"},
        {"pid": 1, "create_time": -1, "path": "x"},
        {"pid": 1, "create_time": 1.0, "path": ""},
        {"pid": "1", "create_time": 1.0, "path": "x"},
        "not json",
        None,
    ):
        with pytest.raises(ValueError):
            DesktopInstance.from_json(bad)  # type: ignore[arg-type]


def test_find_returns_only_the_instance_on_that_path(world: World, tmp_path: Path) -> None:
    mine, other = tmp_path / "Report.pbix", tmp_path / "Budget.pbix"
    world.add(111, other)  # the user's own Desktop on an unrelated file
    world.add(222, mine, created=1500.0)
    found = controller(world).find(mine)
    assert found == DesktopInstance(222, 1500.0, mine)
    assert controller(world).find(tmp_path / "Nothing.pbix") is None


def test_find_ignores_a_process_that_is_not_desktop(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    world.add(300, path, name="notepad.exe")  # a recycled pid
    assert controller(world).find(path) is None


def test_find_refuses_two_windows_on_the_same_file(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    world.add(1, path)
    world.add(2, path)
    with pytest.raises(DesktopSessionError, match="more than one window"):
        controller(world).find(path)


def test_find_does_not_return_an_instance_the_bridge_has_not_connected(
    world: World, tmp_path: Path
) -> None:
    path = tmp_path / "Report.pbix"
    world.add(5, path, connected=False)
    assert controller(world).find(path) is None


def test_status_detects_pid_reuse_with_another_create_time(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    instance = world.add(7, path, created=1000.0)
    c = controller(world)
    assert c.status(instance)["pid"] == 7
    world.procs[7].create_time = 1002.5  # the pid now belongs to a newer process
    with pytest.raises(DesktopSessionError, match="restarted outside Studio"):
        c.status(instance)


def test_status_tolerates_a_second_of_clock_noise_but_not_more(
    world: World, tmp_path: Path
) -> None:
    path = tmp_path / "Report.pbix"
    instance = world.add(7, path, created=1000.0)
    world.procs[7].create_time = 1000.9
    assert controller(world).status(instance)["pid"] == 7
    world.procs[7].create_time = 1001.1
    with pytest.raises(DesktopSessionError, match="restarted"):
        controller(world).status(instance)


def test_status_refuses_a_pid_that_is_now_another_program(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.procs[7].name = "chrome.exe"
    with pytest.raises(DesktopSessionError, match="restarted outside Studio"):
        controller(world).status(instance)


def test_status_reports_a_closed_desktop(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.remove(7)
    with pytest.raises(DesktopSessionError, match="was closed"):
        controller(world).status(instance)


def test_status_refuses_when_a_second_window_holds_the_same_file(
    world: World, tmp_path: Path
) -> None:
    path = tmp_path / "Report.pbix"
    instance = world.add(7, path)
    world.add(8, path, created=1100.0)
    with pytest.raises(DesktopSessionError, match="more than one window"):
        controller(world).status(instance)


def test_status_says_the_report_is_gone_after_retrying_the_bridge(
    world: World, tmp_path: Path
) -> None:
    path = tmp_path / "Report.pbix"
    instance = world.add(7, path)
    world.entries[7]["currentFilePath"] = str(tmp_path / "Other.pbix")  # user switched files
    with pytest.raises(DesktopSessionError, match="no longer has this report open"):
        controller(world).status(instance)
    assert world.now == 5  # five one-second retries: the bridge drops entries while busy


def test_status_compares_paths_case_insensitively_on_windows(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    instance = world.add(7, str(path).upper())
    assert controller(world).status(instance)["pid"] == 7


def test_adopt_reattaches_only_when_pid_create_time_and_path_all_match(
    world: World, tmp_path: Path
) -> None:
    path = tmp_path / "Report.pbix"
    saved = world.add(9, path, created=1234.5)
    c = controller(world)
    assert c.adopt(saved) == saved
    assert c.adopt(saved.to_json()) == saved
    assert c.adopt(json.dumps(saved.to_json())) == saved
    # create time within a second is the same process (float rounding through JSON)
    assert c.adopt(DesktopInstance(9, 1234.0, path)) is not None
    assert c.adopt(DesktopInstance(9, 1236.0, path)) is None  # pid reuse
    assert c.adopt(DesktopInstance(10, 1234.5, path)) is None  # no such pid
    assert c.adopt(DesktopInstance(9, 1234.5, tmp_path / "Other.pbix")) is None  # other file
    assert c.adopt({"pid": "x"}) is None  # not an identity at all
    world.entries[9]["bridgeStatus"] = "connecting"
    assert c.adopt(saved) is None  # not connected yet


def test_adopt_refuses_a_recycled_pid_and_a_duplicate_window(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    saved = world.add(9, path)
    world.procs[9].name = "explorer.exe"
    assert controller(world).adopt(saved) is None
    world.procs[9].name = PBIX_NAME
    world.add(10, path, created=1300.0)
    with pytest.raises(DesktopSessionError, match="more than one window"):
        controller(world).adopt(saved)


def test_adopt_can_restore_the_normalized_marker(world: World, tmp_path: Path) -> None:
    saved = world.add(9, tmp_path / "Report.pbix")
    c = controller(world)
    assert c.adopt(saved) == saved
    assert not c.is_normalized(saved)
    c.adopt(saved, normalized=True)
    assert c.is_normalized(saved)


@pytest.mark.parametrize("action", ["save", "close", "screenshot", "apply"])
def test_every_action_reverifies_the_process_before_touching_anything(
    world: World, tmp_path: Path, action: str
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, created=1000.0)
    world.procs[7].create_time = 5000.0  # a different process now owns the pid
    c = controller(world)
    operations = {
        "save": lambda: c.save(instance),
        "close": lambda: c.close(instance),
        "screenshot": lambda: c.screenshot(instance, "p1", tmp_path / "s.png"),
        "apply": lambda: c.apply_measure_operations(instance, [], {}),
    }
    with pytest.raises(DesktopSessionError, match="restarted outside Studio"):
        operations[action]()
    assert world.uia_calls == []
    assert world.close_posts == []
    assert [call for call in world.bridge_calls if call[0] not in ("status",)] == []


def test_every_action_refuses_two_windows_on_the_file(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path)
    world.add(8, path, created=1100.0)
    c = controller(world)
    for attempt in (
        lambda: c.save(instance),
        lambda: c.close(instance),
        lambda: c.screenshot(instance, "p1", tmp_path / "s.png"),
        lambda: c.apply_measure_operations(instance, [], {}),
    ):
        with pytest.raises(DesktopSessionError, match="more than one window"):
            attempt()
    assert world.uia_calls == []
    assert world.close_posts == []


def test_automation_is_scoped_to_the_verified_pid_only(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.add(1, tmp_path / "Budget.pbix")  # the user's own Desktop
    instance = world.add(2, path)
    world.on_invoke = lambda pid, automation_id: _rewrite(path, "b", world, pid)
    controller(world).save(instance)
    assert {pid for _, pid, _ in world.uia_calls} == {2}


def test_screenshot_verifies_then_captures_that_pid(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    out = controller(world).screenshot(instance, "ReportSection1", tmp_path / "shot.png")
    assert out.read_bytes() == b"x" * 200
    call = next(c for c in world.bridge_calls if c[0] == "screenshot")
    assert call[call.index("--pid") + 1] == "7"


def test_manifest_verifies_identity_first(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    c = controller(world)
    assert c.manifest(instance)["manifest"] == {"methods": []}
    world.procs[7].create_time = 9999.0
    with pytest.raises(DesktopSessionError):
        c.manifest(instance)


# --- launching ----------------------------------------------------------------------------------


def test_launch_argv_and_flags_are_exact(tmp_path: Path) -> None:
    assert launch_argv(r"C:\P F\PBIDesktop.exe", Path(r"C:\my dir\a.pbix")) == [
        "cmd.exe",
        "/c",
        "start",
        "",
        "/B",
        r"C:\P F\PBIDesktop.exe",
        r"C:\my dir\a.pbix",
    ]
    assert launch_flags(breakaway=False) == CREATE_NO_WINDOW == 0x08000000
    assert launch_flags(breakaway=True) == 0x08000000 | 0x01000000
    assert CREATE_BREAKAWAY_FROM_JOB == 0x01000000
    # `start` sees a title, the exe and the file, each quoted by us rather than by subprocess
    line = launch_command_line(r"C:\P F\PBIDesktop.exe", Path(r"C:\my dir\a.pbix"))
    assert line == r'cmd.exe /c start "" /B "C:\P F\PBIDesktop.exe" "C:\my dir\a.pbix"'


def test_launch_command_line_quotes_a_path_without_spaces_that_cmd_would_split() -> None:
    # list2cmdline leaves this bare, so cmd would run "Marketing.pbix" as a second command
    line = launch_command_line(EXE, Path(r"C:\r\Sales&Marketing^1.pbix"))
    assert line.endswith(r' "C:\r\Sales&Marketing^1.pbix"')
    assert subprocess.list2cmdline(["x", r"C:\r\Sales&Marketing^1.pbix"]) == (
        r"x C:\r\Sales&Marketing^1.pbix"
    )


def test_launch_hands_cmd_one_preformatted_line_not_a_list(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Sales&Marketing.pbix"
    controller(world).launch(path)
    (call,) = world.popen_calls
    assert call["argv"] == f'cmd.exe /c start "" /B "{EXE}" "{path}"'


def test_controller_launch_argv_uses_its_own_desktop_exe(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    assert controller(world).launch_argv(path) == launch_argv(EXE, path)


def test_launch_takes_the_breakaway_route_first(world: World, tmp_path: Path) -> None:
    path = tmp_path / "Report.pbix"
    c = controller(world)
    assert c.launch(path) == "breakaway"
    (call,) = world.popen_calls
    assert call["argv"] == launch_command_line(EXE, path)
    assert call["creationflags"] == launch_flags(breakaway=True)
    assert call["stdin"] == call["stdout"] == call["stderr"] == subprocess.DEVNULL
    assert call["close_fds"] is True
    assert c.last_launch_route == "breakaway"
    assert c.launch_survives_parent() is True


def test_launch_skips_breakaway_when_the_job_forbids_it(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(desktop_session, "job_limit_flags", lambda: 0x2000)  # kill-on-close only
    c = controller(world)
    assert c.launch(tmp_path / "Report.pbix") == "cmd"
    assert [call["creationflags"] for call in world.popen_calls] == [launch_flags(breakaway=False)]
    assert c.launch_survives_parent() is False


def test_launch_falls_back_to_plain_cmd_when_breakaway_is_refused(
    world: World, tmp_path: Path
) -> None:
    world.popen_errors = [PermissionError(5, "Access is denied")]
    c = controller(world)
    assert c.launch(tmp_path / "Report.pbix") == "cmd"
    assert [call["creationflags"] for call in world.popen_calls] == [
        launch_flags(breakaway=True),
        launch_flags(breakaway=False),
    ]


def test_launch_reports_when_no_route_starts(world: World, tmp_path: Path) -> None:
    world.popen_errors = [OSError("no"), OSError("no")]
    with pytest.raises(DesktopSessionError, match="could not be started"):
        controller(world).launch(tmp_path / "Report.pbix")


def test_launch_treats_a_failing_start_as_no_route(world: World, tmp_path: Path) -> None:
    world.popen_returncode = 1  # cmd could not start the program
    with pytest.raises(DesktopSessionError, match="could not be started"):
        controller(world).launch(tmp_path / "Report.pbix")
    assert len(world.popen_calls) == 2


def test_launch_does_not_start_a_second_desktop_after_a_nonzero_exit_that_started_one(
    world: World, tmp_path: Path
) -> None:
    world.popen_returncode = 1
    world.on_launch = lambda target: world.spawn(target)
    c = controller(world)
    assert c.launch(tmp_path / "Report.pbix") == "breakaway"
    assert len(world.popen_calls) == 1
    assert len(world.procs) == 1  # one Desktop, not two


@pytest.mark.parametrize("name", ["100%.pbix", "wow!.pbix", 'say"hi".pbix'])
def test_launch_refuses_names_cmd_would_expand(world: World, tmp_path: Path, name: str) -> None:
    with pytest.raises(DesktopSessionError):
        controller(world).launch(tmp_path / name)
    assert world.popen_calls == []


@pytest.mark.parametrize(
    ("flags", "expected"),
    [(None, True), (0, False), (0x2000, False), (0x800, True), (0x1000, True), (0x3800, True)],
)
def test_breakaway_permitted_reads_the_job_limit_flags(
    monkeypatch: pytest.MonkeyPatch, flags: int | None, expected: bool
) -> None:
    monkeypatch.setattr(desktop_session, "job_limit_flags", lambda: flags)
    assert desktop_session.breakaway_permitted() is expected


def test_job_limit_flags_reads_this_process_without_launching_anything() -> None:
    flags = desktop_session.job_limit_flags()
    assert flags is None or isinstance(flags, int)


# --- open ---------------------------------------------------------------------------------------


def test_open_launches_directly_and_picks_the_new_pid_not_the_users(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.add(111, tmp_path / "Budget.pbix")  # the user's Desktop, connected the whole time
    world.on_launch = lambda target: world.spawn(target)
    instance = controller(world).open(path, normalize=False)
    assert instance.pid == world.next_pid and instance.pid != 111
    assert instance.path == path
    assert instance.create_time == 2000.0 + instance.pid
    assert [call["argv"] for call in world.popen_calls] == [launch_command_line(EXE, path)]
    assert not [call for call in world.bridge_calls if call[0] == "open"]


def test_open_reuses_the_instance_that_already_has_the_file(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    existing = world.add(222, path, created=1500.0)
    assert controller(world).open(path) == existing
    assert world.popen_calls == [] and world.uia_calls == []


def test_open_waits_for_a_loading_instance_instead_of_starting_a_second_window(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.add(222, path, connected=False)

    def connect() -> None:
        if world.now >= 3:
            world.entries[222]["bridgeStatus"] = "connected"

    world.on_sleep.append(connect)
    instance = controller(world).open(path, normalize=False)
    assert instance.pid == 222
    assert world.popen_calls == []


def test_open_refuses_two_windows_already_on_the_file(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.add(1, path)
    world.add(2, path)
    with pytest.raises(DesktopSessionError, match="more than one window"):
        controller(world).open(path)


def test_open_falls_back_to_the_bridge_when_no_direct_route_starts(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.add(111, tmp_path / "Budget.pbix")
    world.popen_errors = [OSError("no"), OSError("no")]
    world.on_bridge_open = lambda target: world.spawn(target)
    c = controller(world)
    instance = c.open(path, normalize=False)
    # the bridge printed pid 1; the instance is still the new process that connected
    assert instance.pid == world.next_pid
    assert c.last_launch_route == "bridge"
    (opened,) = [call for call in world.bridge_calls if call[0] == "open"]
    assert opened[1] == str(path)


def test_open_falls_back_to_the_bridge_when_the_direct_launch_starts_nothing(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.on_bridge_open = lambda target: world.spawn(target)
    # Popen "works" but no Desktop ever appears
    instance = controller(world).open(path, normalize=False)
    assert instance.pid == world.next_pid
    assert world.now >= 45
    assert len(world.popen_calls) == 1
    assert len([call for call in world.bridge_calls if call[0] == "open"]) == 1


def test_open_never_opens_a_second_window_when_desktop_started_but_did_not_connect(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.on_launch = lambda target: world.spawn(target, connect_after=10_000)
    with pytest.raises(DesktopSessionError, match="did not open the report in time"):
        controller(world).open(path, timeout=60, normalize=False)
    assert not [call for call in world.bridge_calls if call[0] == "open"]


def test_open_refuses_when_two_new_instances_show_up_on_the_file(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")

    def launch(target: str) -> None:
        world.spawn(target, connect_after=5)
        world.spawn(target, connect_after=5)

    world.on_launch = launch
    with pytest.raises(DesktopSessionError, match="more than one window"):
        controller(world).open(path, normalize=False)


def test_open_ignores_a_new_instance_on_another_file(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")

    def launch(target: str) -> None:
        world.spawn(str(tmp_path / "Budget.pbix"), connect_after=1)  # the user opens their own
        world.spawn(target, connect_after=3)

    world.on_launch = launch
    instance = controller(world).open(path, normalize=False)
    assert Path(world.entries[instance.pid]["currentFilePath"]) == path


def test_open_survives_bridge_hiccups_while_desktop_starts(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.on_launch = lambda target: world.spawn(target, connect_after=6)
    real = world.bridge

    def flaky(*args: str, timeout: int = 60) -> dict[str, Any]:
        # right after launch the bridge cannot answer for a few seconds
        if args[0] == "status" and world.popen_calls and world.now < 3:
            raise DesktopSessionError("The Power BI Desktop bridge failed: not ready")
        return real(*args, timeout=timeout)

    c = controller(world)
    c._bridge = flaky  # type: ignore[method-assign]
    assert c.open(path, normalize=False).pid == world.next_pid


# --- normalization ------------------------------------------------------------------------------


def _save_hooks(world: World, path: Path, *, write_after: float, flag_after: float) -> None:
    """Desktop saves after ``invoke``: file rewritten after ``write_after`` s, flag later."""
    state: dict[str, Any] = {}

    def on_invoke(pid: int, automation_id: str) -> None:
        state["t"] = world.now

    def hook() -> None:
        if "t" not in state:
            return
        elapsed = world.now - state["t"]
        if elapsed >= write_after and not state.get("written"):
            _rewrite(path, "z", world, None)
            state["written"] = True
        if elapsed >= flag_after and state.get("written"):
            for entry in world.entries.values():
                if Path(entry["currentFilePath"]) == path:
                    entry["hasUnsavedChanges"] = False

    world.on_invoke = on_invoke
    world.on_sleep.append(hook)


def _rewrite(path: Path, tag: str, world: World, pid: int | None) -> None:
    before = path.stat().st_mtime_ns if path.exists() else 0
    make_pbix(path, tag)
    # The fake clock makes a rewrite land in the same file-time tick as the file it replaces
    # (Windows timestamps advance in ~15 ms steps), which a real save, seconds later, never does.
    stat = path.stat()
    if stat.st_mtime_ns <= before:
        os.utime(path, ns=(stat.st_atime_ns, before + 2_000_000_000))
    if pid is not None and pid in world.entries:
        world.entries[pid]["hasUnsavedChanges"] = False


def test_open_normalizes_after_desktops_own_dirtying_and_marks_it(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    dirty_at = 4.0

    def on_launch(target: str) -> None:
        pid = world.spawn(target, connect_after=1)

        fired: list[bool] = []

        def hook() -> None:
            if world.now >= dirty_at and pid in world.entries and not fired:
                world.entries[pid]["hasUnsavedChanges"] = True
                fired.append(True)

        world.on_sleep.append(hook)

    world.on_launch = on_launch
    _save_hooks(world, path, write_after=1.0, flag_after=2.0)
    c = controller(world)
    instance = c.open(path)
    assert [call[0] for call in world.uia_calls if call[0] == "invoke"] == ["invoke"]
    assert world.entries[instance.pid]["hasUnsavedChanges"] is False
    assert c.is_normalized(instance) is True


def test_open_without_normalize_leaves_the_flag_ambiguous(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.on_launch = lambda target: world.spawn(target, dirty=True)
    c = controller(world)
    instance = c.open(path, normalize=False)
    assert c.is_normalized(instance) is False
    assert [call for call in world.uia_calls if call[0] == "invoke"] == []


def test_open_that_never_sees_the_dirtying_does_not_claim_normalization(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    world.on_launch = lambda target: world.spawn(target)
    c = controller(world)
    instance = c.open(path)  # stays clean for the whole 25 s settle window
    assert c.is_normalized(instance) is False
    assert [call for call in world.uia_calls if call[0] == "invoke"] == []


def test_reusing_an_open_instance_does_not_mark_it_normalized(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    existing = world.add(222, path, dirty=True)
    c = controller(world)
    assert c.open(path) == existing
    assert c.is_normalized(existing) is False  # the user's work may be in there


# --- save ---------------------------------------------------------------------------------------


def test_save_returns_when_flag_false_file_changed_and_package_opens(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix", "a")
    before = sha256_file(path)
    instance = world.add(7, path, dirty=True)
    _save_hooks(world, path, write_after=2.0, flag_after=3.0)
    result = controller(world).save(instance)
    assert isinstance(result, SaveResult)
    assert result.path == path
    assert result.sha256 == sha256_file(path) != before
    assert result.changed is True
    assert 3.0 <= result.seconds <= 4.0
    assert world.uia_calls[0] == ("windows", 7, "")  # dialogs are checked before pressing Save
    assert ("invoke", 7, "save") in world.uia_calls


def test_save_waits_for_the_flag_even_when_the_file_is_already_written(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix", "a")
    instance = world.add(7, path, dirty=True)
    _save_hooks(world, path, write_after=1.0, flag_after=20.0)
    result = controller(world).save(instance)
    assert result.seconds >= 20.0
    assert world.entries[7]["hasUnsavedChanges"] is False


def test_save_does_not_accept_a_cleared_flag_before_the_file_is_written(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix", "a")
    instance = world.add(7, path, dirty=True)

    def on_invoke(pid: int, automation_id: str) -> None:
        world.entries[7]["hasUnsavedChanges"] = False  # flag first ...
        started = world.now

        def hook() -> None:
            if world.now - started >= 6 and not world.entries[7].get("wrote"):
                world.entries[7]["wrote"] = True
                _rewrite(path, "z", world, None)  # ... file 6 s later

        world.on_sleep.append(hook)

    world.on_invoke = on_invoke
    result = controller(world).save(instance)
    assert result.changed is True
    assert result.seconds >= 6


def test_save_of_a_clean_report_returns_unchanged_after_a_short_wait(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=False)
    result = controller(world).save(instance)
    assert result.changed is False
    assert result.sha256 == sha256_file(path)
    assert 3.0 <= result.seconds < 5.0


def test_save_of_a_clean_report_tolerates_a_disabled_save_button(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=False)
    world.invoke_error = desktop_uia.DesktopAutomationError("Save button is disabled")
    result = controller(world).save(instance)
    assert result.changed is False and result.sha256 == sha256_file(path)


def test_save_of_a_dirty_report_fails_plainly_when_save_cannot_be_pressed(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)
    world.invoke_error = desktop_uia.DesktopAutomationError("no button")
    with pytest.raises(DesktopSessionError, match="could not press Save"):
        controller(world).save(instance)


def test_save_refuses_while_a_dialog_is_up_and_presses_nothing(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)
    world.windows[7] = [dict(MAIN, enabled=False)]
    with pytest.raises(DesktopSessionError, match="showing a dialog") as caught:
        controller(world).save(instance)
    # typed, so a caller reads the title instead of parsing the message
    assert isinstance(caught.value, DesktopDialogOpen)
    assert caught.value.dialog == "a dialog in Power BI Desktop"
    assert [call for call in world.uia_calls if call[0] == "invoke"] == []


def test_save_stops_when_a_dialog_opens_during_the_save(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)

    def on_invoke(pid: int, automation_id: str) -> None:
        world.windows[7] = [dict(MAIN, enabled=False)]

    world.on_invoke = on_invoke
    with pytest.raises(DesktopSessionError, match="opened a dialog while saving") as caught:
        controller(world).save(instance)
    assert isinstance(caught.value, DesktopDialogOpen)
    assert caught.value.dialog == "a dialog in Power BI Desktop"


def test_save_rejects_a_file_that_is_not_a_readable_package(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)

    def on_invoke(pid: int, automation_id: str) -> None:
        path.write_bytes(b"this is not a zip file at all" * 5)
        world.entries[7]["hasUnsavedChanges"] = False

    world.on_invoke = on_invoke
    with pytest.raises(DesktopSessionError, match="failed Studio's check"):
        controller(world).save(instance, timeout=20)


def test_save_rejects_an_archive_whose_data_fails_its_checksum(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)

    def on_invoke(pid: int, automation_id: str) -> None:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("DataModel", b"AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        data = bytearray(path.read_bytes())
        data[data.index(b"AAAAAAAA")] = ord("B")  # one flipped bit of member data
        path.write_bytes(bytes(data))
        world.entries[7]["hasUnsavedChanges"] = False

    world.on_invoke = on_invoke
    with pytest.raises(DesktopSessionError, match=r"failed Studio's check \(part DataModel"):
        controller(world).save(instance, timeout=20)


def test_save_times_out_when_desktop_never_writes(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)  # flag stays set, file never changes
    with pytest.raises(DesktopSessionError, match="did not finish saving in time"):
        controller(world).save(instance, timeout=12)


def test_save_of_a_dirty_report_whose_flag_clears_without_a_write_reports_no_change(
    world: World, tmp_path: Path
) -> None:
    path = make_pbix(tmp_path / "Report.pbix")
    instance = world.add(7, path, dirty=True)
    world.on_invoke = lambda pid, aid: world.entries[7].update(hasUnsavedChanges=False)
    result = controller(world).save(instance)
    assert result.changed is False
    assert result.sha256 == sha256_file(path)


# --- blocking windows ---------------------------------------------------------------------------


def test_blocking_windows_is_empty_for_a_healthy_main_window(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    assert controller(world).blocking_windows(instance) == []


def test_blocking_windows_reports_a_disabled_main_window_as_a_dialog(
    world: World, tmp_path: Path
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.windows[7] = [dict(MAIN, enabled=False)]
    c = controller(world)
    assert c.blocking_windows(instance) == ["a dialog in Power BI Desktop"]
    assert c._blocking_windows(instance) == ["a dialog in Power BI Desktop"]  # old name kept


def test_blocking_windows_names_extra_top_level_windows(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.windows[7] = [
        {"name": "Sign in", "enabled": True, "buttons": []},
        dict(MAIN),
        {"name": "", "enabled": True, "buttons": []},
    ]
    assert controller(world).blocking_windows(instance) == ["Sign in", "a dialog"]


def test_blocking_windows_treats_several_windows_without_a_ribbon_as_blocking(
    world: World, tmp_path: Path
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.windows[7] = [
        {"name": "Power BI Desktop", "enabled": True, "buttons": []},
        {"name": "Loading", "enabled": True, "buttons": []},
    ]
    assert controller(world).blocking_windows(instance) == ["Power BI Desktop", "Loading"]


def test_blocking_windows_accepts_a_lone_window_without_a_ribbon(
    world: World, tmp_path: Path
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.windows[7] = [{"name": "Report", "enabled": True, "buttons": []}]
    assert controller(world).blocking_windows(instance) == []
    world.windows[7] = [{"name": "Report", "enabled": False, "buttons": []}]
    assert controller(world).blocking_windows(instance) == ["a dialog in Power BI Desktop"]


def test_blocking_windows_turns_an_automation_failure_into_a_session_error(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")

    def broken(pid: int) -> list[dict[str, Any]]:
        raise desktop_uia.DesktopAutomationError("boom")

    monkeypatch.setattr(desktop_session.desktop_uia, "windows", broken)
    with pytest.raises(DesktopSessionError, match="could not check whether"):
        controller(world).blocking_windows(instance)


# --- close --------------------------------------------------------------------------------------


def test_close_posts_wm_close_and_waits_for_the_process_to_exit(
    world: World, tmp_path: Path
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.exit_on_wait.add(7)
    c = controller(world)
    c.mark_normalized(instance)
    c.close(instance)
    assert world.close_posts == [7]
    assert 7 not in world.entries
    assert c.is_normalized(instance) is False


def test_close_refuses_unsaved_work_without_posting_anything(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix", dirty=True)
    with pytest.raises(UnsavedDesktopWork):
        controller(world).close(instance)
    assert world.close_posts == []


def test_close_refuses_while_a_dialog_is_open_and_names_it(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.windows[7] = [dict(MAIN), {"name": "Export data", "enabled": True, "buttons": []}]
    with pytest.raises(DesktopSessionError, match="Export data") as caught:
        controller(world).close(instance)
    assert isinstance(caught.value, DesktopDialogOpen) and caught.value.dialog == "Export data"
    assert world.close_posts == []
    assert world.procs[7].alive


def test_close_leaves_desktop_alone_when_it_asks_to_save(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")

    def prompt(pid: int) -> None:
        world.windows[pid] = [dict(MAIN, enabled=False)]  # what WM_CLOSE did on a dirty report

    world.on_post_close = prompt
    with pytest.raises(DesktopSessionError, match="asking a question before it closes") as caught:
        controller(world).close(instance)
    assert isinstance(caught.value, DesktopDialogOpen)
    assert caught.value.dialog == "a dialog in Power BI Desktop"
    assert world.close_posts == [7]
    assert world.procs[7].alive and 7 in world.entries  # untouched, and FakeProcess forbids kill
    assert world.waits == [(7, 2.0)]  # reported after the first poll, not after 60 s


def test_close_reports_a_process_that_never_exits_without_forcing_it(
    world: World, tmp_path: Path
) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    with pytest.raises(DesktopSessionError, match="did not close"):
        controller(world).close(instance, timeout=6)
    assert world.procs[7].alive
    assert [t for _, t in world.waits] == [2.0, 2.0, 2.0]


def test_close_reports_a_window_it_could_not_find(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.post_close_result = 0
    with pytest.raises(DesktopSessionError, match="could not find Power BI Desktop's window"):
        controller(world).close(instance)
    assert world.procs[7].alive


def test_close_checks_identity_before_posting(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.procs[7].create_time = 4000.0
    with pytest.raises(DesktopSessionError, match="restarted outside Studio"):
        controller(world).close(instance)
    assert world.close_posts == []


def test_close_of_an_already_closed_desktop_says_so(world: World, tmp_path: Path) -> None:
    instance = world.add(7, tmp_path / "Report.pbix")
    world.remove(7)
    with pytest.raises(DesktopSessionError, match="was closed"):
        controller(world).close(instance)


def test_no_module_can_end_a_process_by_force() -> None:
    forbidden = re.compile(
        r"\.kill\(|\.terminate\(|Stop-Process|TerminateProcess|taskkill|os\.kill|killpg",
        re.IGNORECASE,
    )
    for module in (desktop_session, desktop_uia):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert forbidden.findall(source) == [], module.__name__


# --- version and readiness ----------------------------------------------------------------------


def _store_exe(root: Path, version: str = "2.157.1354.0") -> Path:
    exe = (
        root / f"Microsoft.MicrosoftPowerBIDesktop_{version}_x64__8wekyb3d8bbwe" / "bin" / PBIX_NAME
    )
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ not a real executable")
    return exe


def test_version_from_path_reads_the_store_folder_name() -> None:
    assert desktop_session.version_from_path(EXE) == "2.157.1354.0"
    assert desktop_session.version_from_path(r"C:\Tools\PBIDesktop.exe") is None


def test_version_falls_back_to_the_folder_name_and_is_cached(tmp_path: Path) -> None:
    exe = _store_exe(tmp_path)
    c = DesktopController(["node", "b.js"], "pbi", str(exe))
    assert c.version() == "2.157.1354.0"
    exe.unlink()
    assert c.version() == "2.157.1354.0"  # cached


def test_version_is_none_without_an_installed_desktop(tmp_path: Path) -> None:
    c = DesktopController(["node", "b.js"], "pbi", str(tmp_path / "missing" / PBIX_NAME))
    assert c.version() is None


def test_file_version_reads_a_real_executable_or_gives_up(tmp_path: Path) -> None:
    fake = tmp_path / "x.exe"
    fake.write_bytes(b"not a portable executable")
    assert desktop_session._file_version(fake) is None


def _receipt(folder: Path, version: str, *, passed: bool, inside: str | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    body = {"desktop_version": inside or version, "passed": passed}
    (folder / f"desktop-{version}.json").write_text(json.dumps(body), encoding="utf-8")


def _ready_controller(tmp_path: Path, receipts: Path) -> DesktopController:
    exe = _store_exe(tmp_path)
    bridge_js = tmp_path / "bridge.js"
    bridge_js.write_text("//", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_bytes(b"")
    pbi = tmp_path / "pbi.exe"
    pbi.write_bytes(b"")
    return DesktopController([str(node), str(bridge_js)], str(pbi), str(exe), receipts_dir=receipts)


def test_readiness_needs_a_passing_receipt_for_the_installed_version(tmp_path: Path) -> None:
    receipts = tmp_path / "evidence"
    c = _ready_controller(tmp_path, receipts)
    _receipt(receipts, "2.157.1354.0", passed=True)
    ready = c.readiness()
    assert ready.bridge_ok and ready.pbi_ok and ready.desktop_ok and ready.contract_passed
    assert ready.version == "2.157.1354.0"
    assert ready.reasons == []
    assert ready.can_edit_model is True


def test_readiness_without_a_receipt_keeps_model_edits_off(tmp_path: Path) -> None:
    c = _ready_controller(tmp_path, tmp_path / "evidence")
    ready = c.readiness()
    assert ready.desktop_ok and ready.bridge_ok and ready.pbi_ok
    assert ready.contract_passed is False and ready.can_edit_model is False
    assert ready.reasons == ["Power BI Desktop 2.157.1354.0 has not been checked with Studio yet"]


def test_readiness_with_a_failed_receipt_says_so(tmp_path: Path) -> None:
    receipts = tmp_path / "evidence"
    c = _ready_controller(tmp_path, receipts)
    _receipt(receipts, "2.157.1354.0", passed=False)
    ready = c.readiness()
    assert ready.can_edit_model is False
    assert ready.reasons == ["Power BI Desktop 2.157.1354.0 failed Studio's compatibility check"]


def test_readiness_ignores_a_receipt_for_another_desktop_version(tmp_path: Path) -> None:
    receipts = tmp_path / "evidence"
    c = _ready_controller(tmp_path, receipts)
    _receipt(receipts, "2.158.0.0", passed=True)  # Desktop updated: the old receipt is stale
    assert c.readiness().contract_passed is False
    _receipt(receipts, "2.157.1354.0", passed=True, inside="2.150.0.0")  # renamed file
    assert c.readiness().contract_passed is False


def test_readiness_reports_missing_tools(tmp_path: Path) -> None:
    c = DesktopController(
        [str(tmp_path / "node.exe"), str(tmp_path / "bridge.js")],
        str(tmp_path / "pbi.exe"),
        str(tmp_path / "PBIDesktop.exe"),
        receipts_dir=tmp_path,
    )
    ready = c.readiness()
    assert (ready.bridge_ok, ready.pbi_ok, ready.desktop_ok, ready.contract_passed) == (
        False,
        False,
        False,
        False,
    )
    assert ready.version is None
    assert ready.reasons == [
        "The Power BI Desktop bridge is not installed",
        "The Power BI command-line tool is not installed",
        "Power BI Desktop is not installed",
    ]


def test_read_receipt_refuses_unsafe_version_strings(tmp_path: Path) -> None:
    assert desktop_session.read_receipt("..\\evil", tmp_path) is None
    assert desktop_session.read_receipt("", tmp_path) is None
    assert desktop_session.read_receipt(None, tmp_path) is None
    (tmp_path / "desktop-1.0.json").write_text("not json", encoding="utf-8")
    assert desktop_session.read_receipt("1.0", tmp_path) is None


def test_default_receipts_dir_is_beside_the_contract_script() -> None:
    folder = desktop_session.default_receipts_dir()
    assert folder.parts[-3:] == ("evals", "imported_edit", "evidence")


# --- model edits: rollback on every kind of failure ---------------------------------------------


class PbiLog:
    """A fake pbi-cli: records every call, answers with JSON, fails on request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.timeout_on: set[int] = set()  # 1-based call numbers that never answer
        self.broken_on: set[int] = set()  # 1-based call numbers where the tool cannot be run
        self.fail_when: Callable[[tuple[str, ...]], bool] = lambda args: False

    def run(self, argv: list[str], **kwargs: Any) -> Any:
        args = tuple(a for a in argv[1:] if a != "--json")
        self.calls.append(args)
        if len(self.calls) in self.timeout_on:
            raise subprocess.TimeoutExpired(argv, 120)
        if len(self.calls) in self.broken_on:
            raise FileNotFoundError(2, "pbi.exe")
        if self.fail_when(args):
            return types.SimpleNamespace(
                stdout=json.dumps({"error": "rejected"}), stderr="", returncode=1
            )
        payload: dict[str, Any] = {}
        if args[:2] == ("-c", "conn") and args[2:4] == ("measure", "get"):
            payload = {"expression": "1", "formatString": "", "displayFolder": ""}
        return types.SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)

    def verbs(self) -> list[tuple[str, ...]]:
        return [a[2:5] for a in self.calls if a[:2] == ("-c", "conn")]


@pytest.fixture
def pbi(world: World, monkeypatch: pytest.MonkeyPatch) -> PbiLog:
    log = PbiLog()
    monkeypatch.setattr(desktop_session.subprocess, "run", log.run, raising=False)
    monkeypatch.setattr(desktop_session.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    return log


def edit_controller(world: World, tmp_path: Path) -> tuple[DesktopController, DesktopInstance]:
    instance = world.add(7, make_pbix(tmp_path / "Report.pbix"))
    c = controller(world)
    c._connection = lambda inst: "conn"  # type: ignore[method-assign]
    return c, instance


def create(name: str) -> Any:
    return desktop_session.CreateMeasure(table="T", name=name, expression="1")


def test_a_missing_home_table_is_a_session_error_and_earlier_ops_are_undone(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    tables: dict[str, str] = {}
    operations = [create("A"), desktop_session.UpdateMeasure(name="Missing", expression="2")]
    with pytest.raises(DesktopSessionError, match=r"\[Missing\]"):
        c.apply_measure_operations(instance, operations, tables)
    assert ("measure", "delete", "A") in pbi.verbs()
    assert tables == {}


def test_a_pbi_timeout_undoes_the_change_in_flight_and_the_ones_before(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    # calls: 1 create A, 2 probe A, 3 create B (never answers)
    pbi.timeout_on = {3}
    with pytest.raises(DesktopSessionError, match="did not answer the change in time"):
        c.apply_measure_operations(instance, [create("A"), create("B")], {})
    undone = [v for v in pbi.verbs() if v[:2] == ("measure", "delete")]
    assert undone == [("measure", "delete", "B"), ("measure", "delete", "A")]


def test_an_undo_that_fails_is_reported_and_the_others_still_run(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    pbi.fail_when = lambda args: (
        args[2:5] == ("measure", "delete", "B") or (args[2:5] == ("measure", "create", "C"))
    )
    with pytest.raises(DesktopSessionError) as caught:
        c.apply_measure_operations(instance, [create("A"), create("B"), create("C")], {})
    # B's delete is undo of a create that succeeded; it fails, A's still runs
    assert "could not undo the change to [B]" in str(caught.value)
    assert "Don't save" in str(caught.value)
    assert "[A]" not in str(caught.value)
    assert ("measure", "delete", "A") in pbi.verbs()


def test_a_plain_rejection_needs_no_undo_of_the_rejected_change(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    pbi.fail_when = lambda args: args[2:5] == ("measure", "create", "B")
    with pytest.raises(DesktopSessionError, match="Power BI rejected the change"):
        c.apply_measure_operations(instance, [create("A"), create("B")], {})
    assert [v for v in pbi.verbs() if v[1] == "delete"] == [("measure", "delete", "A")]


def test_an_unexpected_error_is_undone_and_wrapped(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)

    def broken(connection: str, measure: str) -> None:
        raise RuntimeError("boom")

    c._probe = broken  # type: ignore[method-assign]
    with pytest.raises(DesktopSessionError, match="could not apply the change") as caught:
        c.apply_measure_operations(instance, [create("A")], {})
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert ("measure", "delete", "A") in pbi.verbs()


def test_a_truncated_bridge_answer_is_a_session_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def cut_short(argv: list[str], **kwargs: Any) -> Any:
        return types.SimpleNamespace(stdout='{"instances": [', stderr="", returncode=0)

    monkeypatch.setattr(desktop_session.subprocess, "run", cut_short, raising=False)
    monkeypatch.setattr(desktop_session.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    c = DesktopController(["node", "b.js"], "pbi", EXE, clock=world.monotonic, sleep=world.sleep)
    with pytest.raises(DesktopSessionError, match="unreadable answer"):
        c._instances()


def test_a_bridge_that_cannot_start_is_a_session_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "node")

    monkeypatch.setattr(desktop_session.subprocess, "run", missing, raising=False)
    monkeypatch.setattr(desktop_session.subprocess, "CREATE_NO_WINDOW", 0, raising=False)
    c = DesktopController(["node", "b.js"], "pbi", EXE, clock=world.monotonic, sleep=world.sleep)
    with pytest.raises(DesktopSessionError, match="could not be started"):
        c._instances()


# --- the port's own rollback for every failure of the pbi tool ---------------------------------


def update(name: str, expression: str) -> Any:
    return desktop_session.UpdateMeasure(name=name, expression=expression)


def logged_probes(c: DesktopController, pbi: PbiLog, fail_on: str | None = None) -> None:
    """Make the probe a line in the fake pbi's log (``("-c", "conn", "probe", <measure>)``), so a
    test sees when each measure is checked relative to the writes."""

    def probe(connection: str, measure: str) -> None:
        pbi.calls.append(("-c", connection, "probe", measure))
        if measure == fail_on:
            raise DesktopSessionError(f"Power BI rejected the change: [{measure}] has an error")

    c._probe = probe  # type: ignore[method-assign]


def test_a_renames_dependents_are_probed_only_after_every_update_has_landed(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    """The edit layer appends an UpdateMeasure for each measure whose formula used the old name.
    Until its update runs a dependent is broken, and so is a measure that uses it, so probing
    each update right away would fail on a formula that the next update repairs."""
    c, instance = edit_controller(world, tmp_path)
    logged_probes(c, pbi)
    tables = {"R": "T", "D1": "T", "D2": "T"}
    operations = [
        desktop_session.RenameMeasure(name="R", new_name="R2"),
        update("D1", "[R2] + 1"),
        update("D2", "[D1] + [R2]"),
    ]
    done = c.apply_measure_operations(instance, operations, tables)
    assert done == ["renamed [R] to [R2]", "updated [D1]", "updated [D2]"]
    assert pbi.verbs() == [
        ("measure", "rename", "R"),
        ("probe", "R2"),
        ("measure", "get", "D1"),
        ("measure", "update", "D1"),
        ("measure", "get", "D2"),
        ("measure", "update", "D2"),
        ("probe", "D1"),
        ("probe", "D2"),
    ]
    assert tables == {"R2": "T", "D1": "T", "D2": "T"}


def test_updates_without_a_rename_are_still_probed_one_by_one(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    logged_probes(c, pbi)
    c.apply_measure_operations(
        instance, [update("D1", "1"), update("D2", "2")], {"D1": "T", "D2": "T"}
    )
    assert [v[:2] for v in pbi.verbs()] == [
        ("measure", "get"),
        ("measure", "update"),
        ("probe", "D1"),
        ("measure", "get"),
        ("measure", "update"),
        ("probe", "D2"),
    ]


def test_a_deferred_probe_that_fails_undoes_the_updates_and_the_rename(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    logged_probes(c, pbi, fail_on="D2")
    tables = {"R": "T", "D1": "T", "D2": "T"}
    operations = [
        desktop_session.RenameMeasure(name="R", new_name="R2"),
        update("D1", "[R2] + 1"),
        update("D2", "[NoSuchMeasure]"),
    ]
    with pytest.raises(DesktopSessionError, match=r"\[D2\] has an error"):
        c.apply_measure_operations(instance, operations, tables)
    # last in, first out: the two formulas are put back, then the measure gets its name back
    assert pbi.verbs()[-3:] == [
        ("measure", "update", "D2"),
        ("measure", "update", "D1"),
        ("measure", "rename", "R2"),
    ]
    assert tables == {"R": "T", "D1": "T", "D2": "T"}


def test_a_dependent_update_desktop_rejects_undoes_the_rename(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    logged_probes(c, pbi)
    pbi.fail_when = lambda args: args[2:5] == ("measure", "update", "D1") and "-e" in args
    tables = {"R": "T", "D1": "T"}
    with pytest.raises(DesktopSessionError, match="Power BI rejected the change"):
        c.apply_measure_operations(
            instance,
            [desktop_session.RenameMeasure(name="R", new_name="R2"), update("D1", "[R2]")],
            tables,
        )
    assert ("measure", "rename", "R2") in pbi.verbs()  # the rename went back to R
    assert tables == {"R": "T", "D1": "T"}


def test_hiding_a_measure_is_refused_before_anything_is_sent(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    """``pbi measure update`` has no flag for it: accepting the operation would report a change
    that is never made, so it is refused up front (nothing else of the batch runs either)."""
    c, instance = edit_controller(world, tmp_path)
    hide = desktop_session.UpdateMeasure(name="B", hidden=True, expression="2")
    with pytest.raises(DesktopSessionError, match=r"cannot hide or show a measure yet.*\[B\]"):
        c.apply_measure_operations(instance, [create("A"), hide], {"B": "T"})
    assert pbi.calls == []
    unhide = desktop_session.UpdateMeasure(name="B", hidden=False)
    with pytest.raises(DesktopSessionError, match=r"\[B\] in the Data pane and choose Unhide"):
        c.apply_measure_operations(instance, [unhide], {"B": "T"})
    assert pbi.calls == []


def test_only_a_hidden_change_is_unsupported() -> None:
    supported = [
        create("A"),
        update("B", "2"),
        desktop_session.UpdateMeasure(name="B", format_string="0.0"),
        desktop_session.RenameMeasure(name="B", new_name="C"),
        desktop_session.DeleteMeasure(name="B"),
    ]
    assert desktop_session.unsupported_measure_change(supported) is None
    assert desktop_session.unsupported_measure_change([]) is None
    problem = desktop_session.unsupported_measure_change(
        [*supported, desktop_session.UpdateMeasure(name="Sales", hidden=True)]
    )
    assert problem == (
        "Studio cannot hide or show a measure yet. In Power BI Desktop, right-click [Sales] in "
        "the Data pane and choose Hide"
    )


def test_a_pbi_tool_that_cannot_be_run_is_a_session_error_and_the_port_rolls_back(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    # calls: 1 create A, 2 probe A, 3 create B (the tool cannot be started)
    pbi.broken_on = {3}
    with pytest.raises(DesktopSessionError, match="could not run Power BI's command-line tool"):
        c.apply_measure_operations(instance, [create("A"), create("B")], {})
    undone = [v for v in pbi.verbs() if v[:2] == ("measure", "delete")]
    assert undone == [("measure", "delete", "B"), ("measure", "delete", "A")]


def test_a_probe_that_times_out_is_a_session_error_and_the_port_rolls_back(
    world: World, tmp_path: Path, pbi: PbiLog
) -> None:
    c, instance = edit_controller(world, tmp_path)
    pbi.timeout_on = {2}  # calls: 1 create A, 2 the probe of A
    with pytest.raises(DesktopSessionError, match="did not answer the change in time"):
        c.apply_measure_operations(instance, [create("A")], {})
    assert [v for v in pbi.verbs() if v[:2] == ("measure", "delete")] == [
        ("measure", "delete", "A")
    ]


def test_a_probe_that_cannot_write_its_temp_file_rolls_back_as_a_session_error(
    world: World, tmp_path: Path, pbi: PbiLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    c, instance = edit_controller(world, tmp_path)

    def disk_full(*args: Any, **kwargs: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(desktop_session.tempfile, "NamedTemporaryFile", disk_full)
    with pytest.raises(DesktopSessionError, match="temporary file that checks the measure works"):
        c.apply_measure_operations(instance, [create("A")], {})
    assert ("measure", "delete", "A") in pbi.verbs()  # the measure it had just made is undone


def test_save_presses_again_when_desktop_ignored_the_first_press(
    world: World, tmp_path: Path
) -> None:
    """A Save pressed while Desktop still loads the report is ignored (contract run of
    2026-09-29); the controller presses again after a quiet spell instead of timing out."""
    path = make_pbix(tmp_path / "Report.pbix", "a")
    instance = world.add(7, path, dirty=True)
    presses: list[float] = []

    def on_invoke(pid: int, automation_id: str) -> None:
        presses.append(world.now)
        if len(presses) == 2:  # the second press is the one Desktop acts on
            _rewrite(path, "b", world, pid)

    world.on_invoke = on_invoke
    result = controller(world).save(instance)
    assert result.changed is True
    assert len(presses) == 2
    assert presses[1] - presses[0] >= 12.0
    assert world.entries[7]["hasUnsavedChanges"] is False


def test_save_stops_pressing_after_a_few_tries_and_says_so(world: World, tmp_path: Path) -> None:
    path = make_pbix(tmp_path / "Report.pbix", "a")
    instance = world.add(7, path, dirty=True)
    presses: list[float] = []
    world.on_invoke = lambda pid, automation_id: presses.append(world.now)
    with pytest.raises(DesktopSessionError, match="did not finish saving in time"):
        controller(world).save(instance)
    assert len(presses) == 4


def test_save_presses_again_when_desktop_marks_the_report_changed_after_writing_it(
    world: World, tmp_path: Path
) -> None:
    """The untouched original: the first Save writes the file, then Desktop, still loading,
    marks the report changed again. A second press leaves it saved and clean."""
    path = make_pbix(tmp_path / "Report.pbix", "a")
    instance = world.add(7, path, dirty=True)
    presses: list[float] = []

    def on_invoke(pid: int, automation_id: str) -> None:
        presses.append(world.now)
        _rewrite(path, "bc"[len(presses) - 1 : len(presses)] or "z", world, None)
        world.entries[7]["hasUnsavedChanges"] = len(presses) == 1  # dirty again after the first

    world.on_invoke = on_invoke
    result = controller(world).save(instance)
    assert result.changed is True and len(presses) == 2
    assert world.entries[7]["hasUnsavedChanges"] is False
