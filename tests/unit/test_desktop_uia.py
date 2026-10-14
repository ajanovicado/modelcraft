"""desktop_uia without PowerShell: how the command is built and how its answers are read.

The machine's PowerShell runs under a Restricted execution policy that refuses script files,
so the script must go through -EncodedCommand, and a PSExecutionPolicyPreference in the
caller's environment must not reach it. Whether the UI Automation script finds the buttons is
proven live by the contract script, not here.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import types
from typing import Any

import psutil
import pytest

from ai_powerbi_analyst.studio import desktop_uia
from ai_powerbi_analyst.studio.desktop_uia import DesktopAutomationError


class Recorder:
    def __init__(self, stdout: str = "{}", returncode: int = 0, stderr: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.raises: BaseException | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((argv, kwargs))
        if self.raises is not None:
            raise self.raises
        return types.SimpleNamespace(
            stdout=self.stdout, stderr=self.stderr, returncode=self.returncode
        )


@pytest.fixture
def desktop_process(monkeypatch: pytest.MonkeyPatch) -> dict[int, str]:
    """pid -> process name for the fake process table."""
    table: dict[int, str] = {4321: "PBIDesktop.exe"}

    def process(pid: int) -> Any:
        if pid not in table:
            raise psutil.NoSuchProcess(pid)
        return types.SimpleNamespace(name=lambda: table[pid])

    monkeypatch.setattr(psutil, "Process", process)
    return table


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch, desktop_process: dict[int, str]) -> Recorder:
    rec = Recorder(json.dumps({"windows": [], "invoked": None}))
    monkeypatch.setattr(desktop_uia.subprocess, "run", rec)
    return rec


def test_run_uses_encoded_command_and_never_a_script_file(recorder: Recorder) -> None:
    desktop_uia.run(4321, "list")
    (argv, kwargs), *rest = recorder.calls
    assert rest == []
    assert argv[0].lower().endswith(r"system32\windowspowershell\v1.0\powershell.exe")
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-EncodedCommand"]
    assert len(argv) == 5
    assert "-File" not in argv and "-Command" not in argv
    assert base64.b64decode(argv[4]).decode("utf-16-le") == desktop_uia._SCRIPT
    assert kwargs["timeout"] == 60
    assert kwargs["check"] is False
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_run_strips_the_execution_policy_override_from_the_environment(
    recorder: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PSExecutionPolicyPreference", "Bypass")
    monkeypatch.setenv("STUDIO_UIA_ID", "leftover")  # a stale value must not leak through
    desktop_uia.run(4321, "invoke", automation_id="save", name="Save")
    env = recorder.calls[0][1]["env"]
    assert "PSEXECUTIONPOLICYPREFERENCE" not in {key.upper() for key in env}
    assert env["STUDIO_UIA_PID"] == "4321"
    assert env["STUDIO_UIA_ACTION"] == "invoke"
    assert env["STUDIO_UIA_ID"] == "save"
    assert env["STUDIO_UIA_NAME"] == "Save"
    assert env["PATH"] == os.environ["PATH"]  # the rest of the environment is kept


def test_the_script_writes_utf8_so_non_ascii_window_names_survive(recorder: Recorder) -> None:
    # run() decodes stdout as UTF-8; without this line PowerShell writes the OEM code page and
    # "Ünïcode" comes back as U+FFFD (measured 2026-09-28 with the same -EncodedCommand route)
    first_code_line = next(
        line for line in desktop_uia._SCRIPT.splitlines() if line and not line.startswith("#")
    )
    assert "[Console]::OutputEncoding" in first_code_line
    assert "UTF8Encoding($false)" in first_code_line
    desktop_uia.run(4321, "list")
    assert recorder.calls[0][1]["encoding"] == "utf-8"


def test_run_scopes_every_call_to_the_process_id_it_was_given(recorder: Recorder) -> None:
    desktop_uia.run(4321, "list")
    assert "STUDIO_UIA_PID" in desktop_uia._SCRIPT
    assert "ProcessIdProperty" in desktop_uia._SCRIPT
    assert recorder.calls[0][1]["env"]["STUDIO_UIA_PID"] == "4321"


@pytest.mark.parametrize("bad", [0, -1, True, "7", None, 1.5])
def test_run_rejects_a_missing_or_malformed_pid(recorder: Recorder, bad: Any) -> None:
    with pytest.raises(ValueError, match="process id"):
        desktop_uia.run(bad, "list")
    assert recorder.calls == []


def test_run_rejects_unknown_actions(recorder: Recorder) -> None:
    with pytest.raises(ValueError, match="Unknown UI Automation action"):
        desktop_uia.run(4321, "click")
    assert recorder.calls == []


def test_run_refuses_a_pid_that_is_not_power_bi_desktop(
    recorder: Recorder, desktop_process: dict[int, str]
) -> None:
    desktop_process[555] = "chrome.exe"  # a recycled pid
    with pytest.raises(DesktopAutomationError, match="not Power BI Desktop"):
        desktop_uia.run(555, "invoke", automation_id="save")
    with pytest.raises(DesktopAutomationError, match="is not running"):
        desktop_uia.run(999, "list")
    assert recorder.calls == []


def test_run_accepts_the_process_name_in_any_case(
    recorder: Recorder, desktop_process: dict[int, str]
) -> None:
    desktop_process[7] = "pbidesktop.EXE"
    desktop_uia.run(7, "list")
    assert len(recorder.calls) == 1


def test_windows_unwraps_single_item_arrays_powershell_flattens(recorder: Recorder) -> None:
    recorder.stdout = json.dumps(
        {
            "windows": {
                "name": "Report",
                "enabled": True,
                "buttons": {"id": "save", "name": "Save", "enabled": True},
            },
            "invoked": None,
        }
    )
    assert desktop_uia.windows(4321) == [
        {
            "name": "Report",
            "enabled": True,
            "buttons": [{"id": "save", "name": "Save", "enabled": True}],
        }
    ]


def test_windows_of_a_process_without_windows_is_empty(recorder: Recorder) -> None:
    recorder.stdout = json.dumps({"windows": None, "invoked": None})
    assert desktop_uia.windows(4321) == []


def test_invoke_returns_what_was_pressed(recorder: Recorder) -> None:
    recorder.stdout = json.dumps(
        {"windows": [], "invoked": {"id": "save", "name": "Save", "window": "Report"}}
    )
    assert desktop_uia.invoke(4321, automation_id="save") == {
        "id": "save",
        "name": "Save",
        "window": "Report",
    }
    assert recorder.calls[0][1]["env"]["STUDIO_UIA_ACTION"] == "invoke"


def test_invoke_says_when_there_is_no_such_button(recorder: Recorder) -> None:
    with pytest.raises(DesktopAutomationError, match="shows no 'save' button"):
        desktop_uia.invoke(4321, automation_id="save")


def test_invoke_says_when_the_button_is_disabled(recorder: Recorder) -> None:
    recorder.stdout = json.dumps(
        {"windows": [], "invoked": {"id": "save", "name": "Save", "error": "disabled"}}
    )
    with pytest.raises(DesktopAutomationError, match="'save' button is disabled"):
        desktop_uia.invoke(4321, automation_id="save")


def test_invoke_by_name_reports_the_name(recorder: Recorder) -> None:
    with pytest.raises(DesktopAutomationError, match="shows no 'Save' button"):
        desktop_uia.invoke(4321, name="Save")


def test_dump_and_invoke_any_use_their_own_actions(recorder: Recorder) -> None:
    recorder.stdout = json.dumps(
        {
            "windows": [],
            "dump": {
                "type": "Window",
                "name": "Microsoft Power BI Desktop",
                "enabled": True,
                "topLevel": False,
                "buttons": {"id": "", "name": "Don't save", "enabled": True},
            },
        }
    )
    found = desktop_uia.dump(4321)
    assert found == [
        {
            "type": "Window",
            "name": "Microsoft Power BI Desktop",
            "enabled": True,
            "topLevel": False,
            "buttons": [{"id": "", "name": "Don't save", "enabled": True}],
        }
    ]
    assert recorder.calls[-1][1]["env"]["STUDIO_UIA_ACTION"] == "dump"
    assert recorder.calls[-1][1]["timeout"] == 120

    recorder.stdout = json.dumps({"windows": [], "invoked": {"id": "", "name": "Don't save"}})
    assert desktop_uia.invoke_any(4321, name="Don't save") == {"id": "", "name": "Don't save"}
    env = recorder.calls[-1][1]["env"]
    assert (env["STUDIO_UIA_ACTION"], env["STUDIO_UIA_NAME"]) == ("invoke_any", "Don't save")


def test_dump_of_a_process_without_dialogs_is_empty(recorder: Recorder) -> None:
    recorder.stdout = json.dumps({"windows": [], "dump": []})
    assert desktop_uia.dump(4321) == []


def test_the_script_knows_every_action_it_can_be_asked_for() -> None:
    assert set(desktop_uia._ACTIONS) == {"list", "invoke", "dump", "invoke_any"}
    for action in desktop_uia._ACTIONS - {"list", "invoke"}:
        assert f"'{action}'" in desktop_uia._SCRIPT
    assert "$env:STUDIO_UIA_ACTION" in desktop_uia._SCRIPT


def test_a_timeout_becomes_a_plain_automation_error(recorder: Recorder) -> None:
    recorder.raises = subprocess.TimeoutExpired("powershell", 60)
    with pytest.raises(DesktopAutomationError, match="did not respond"):
        desktop_uia.run(4321, "list")


def test_a_missing_powershell_becomes_a_plain_automation_error(recorder: Recorder) -> None:
    recorder.raises = FileNotFoundError("powershell.exe")
    with pytest.raises(DesktopAutomationError, match="could not be started"):
        desktop_uia.run(4321, "list")


def test_a_failed_script_reports_the_tail_of_its_error(recorder: Recorder) -> None:
    recorder.returncode, recorder.stdout, recorder.stderr = 1, "", "x" * 500 + "REAL PROBLEM"
    with pytest.raises(DesktopAutomationError, match="REAL PROBLEM$"):
        desktop_uia.run(4321, "list")


def test_an_empty_answer_is_a_failure(recorder: Recorder) -> None:
    recorder.stdout = ""
    with pytest.raises(DesktopAutomationError, match="no output"):
        desktop_uia.run(4321, "list")


@pytest.mark.parametrize("output", ["not json", "[1, 2]", '"text"'])
def test_unreadable_output_is_a_failure(recorder: Recorder, output: str) -> None:
    recorder.stdout = output
    with pytest.raises(DesktopAutomationError, match="unreadable output"):
        desktop_uia.run(4321, "list")
