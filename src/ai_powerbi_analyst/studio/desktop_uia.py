"""UI Automation for one Power BI Desktop process: Save and the close prompt, no keys.

The Desktop bridge (0.1.2) has no save or close method. On 2026-09-28, SendKeys Ctrl+S
from a background process did not save, because the foreground lock stopped it. A UI
Automation Invoke on the ribbon's Save button (AutomationId "save") saved in about 3 s
without focus. This machine runs PowerShell under a Restricted execution policy, which
refuses script files. So the script runs as -EncodedCommand, which the policy does not
cover, the same way preflight.py runs its commands, and PSExecutionPolicyPreference is
stripped from its environment so a session-level override cannot change that. Every call
is scoped to one process id and prints JSON, and the process must be PBIDesktop.exe: a
recycled pid that now belongs to another program is refused instead of automated.

``list`` and ``invoke`` look at the process's top-level windows (verified live). ``dump``
and ``invoke_any`` search every element the process owns, including dialogs and web-view
roots that are not top-level windows. They exist for the contract script, which has to
find out whether the "Save changes?" prompt (an embedded web view) can be answered at all;
Studio itself never answers that prompt.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from typing import Any

import psutil

DESKTOP_PROCESS = "pbidesktop.exe"
_ACTIONS = frozenset({"list", "invoke", "dump", "invoke_any"})

_SCRIPT = r"""
# Window and button names are not ASCII in every locale; run() decodes stdout as UTF-8,
# but PowerShell writes it in the console's OEM code page unless told otherwise.
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$ae = [System.Windows.Automation.AutomationElement]
$pidValue = [int]$env:STUDIO_UIA_PID
$action = $env:STUDIO_UIA_ACTION
$wanted = $env:STUDIO_UIA_ID
$wantedName = $env:STUDIO_UIA_NAME
$byPid = New-Object System.Windows.Automation.PropertyCondition($ae::ProcessIdProperty, $pidValue)
$buttonType = New-Object System.Windows.Automation.PropertyCondition(
    $ae::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)
$windows = $ae::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, $byPid)
$result = @{ windows = @(); invoked = $null }
if ($action -eq 'dump' -or $action -eq 'invoke_any') {
    $deep = [System.Windows.Automation.TreeScope]::Descendants
    $everything = $ae::RootElement.FindAll($deep, $byPid)
    $topHandles = @{}
    foreach ($w in $windows) { $topHandles[[string]$w.Current.NativeWindowHandle] = $true }
    $dump = @()
    $seen = 0
    foreach ($e in $everything) {
        $seen++
        if ($seen -gt 6000) { break }
        $type = $e.Current.ControlType.ProgrammaticName -replace '^ControlType\.', ''
        $id = $e.Current.AutomationId
        $name = $e.Current.Name
        $class = $e.Current.ClassName
        if ($action -eq 'invoke_any') {
            if ($result.invoked -eq $null -and $type -eq 'Button' -and
                (($wanted -and $id -eq $wanted) -or ($wantedName -and $name -eq $wantedName))) {
                if (-not $e.Current.IsEnabled) {
                    $result.invoked = @{ id = $id; name = $name; error = 'disabled' }
                } else {
                    $invokeId = [System.Windows.Automation.InvokePattern]::Pattern
                    $pattern = $e.GetCurrentPattern($invokeId)
                    $pattern.Invoke()
                    $result.invoked = @{ id = $id; name = $name; window = 'any' }
                }
            }
            continue
        }
        $isRoot = ($type -eq 'Window') -or ($type -eq 'Document') -or
                  ($class -like 'Chrome*') -or ($class -like '*WebView*')
        if (-not $isRoot) { continue }
        $handle = [string]$e.Current.NativeWindowHandle
        $info = @{ type = $type; name = $name; id = $id; className = $class;
                   enabled = $e.Current.IsEnabled;
                   topLevel = ($handle -ne '0' -and $topHandles.ContainsKey($handle));
                   buttons = @() }
        $buttons = $e.FindAll($deep, $buttonType)
        $count = 0
        foreach ($b in $buttons) {
            $count++
            if ($count -gt 200) { break }
            $bid = $b.Current.AutomationId
            $bname = $b.Current.Name
            if ($bid -or $bname) {
                $info.buttons += @{ id = $bid; name = $bname; enabled = $b.Current.IsEnabled }
            }
        }
        $dump += $info
    }
    $result.dump = $dump
    $result | ConvertTo-Json -Depth 6 -Compress
    exit 0
}
foreach ($w in $windows) {
    $info = @{ name = $w.Current.Name; className = $w.Current.ClassName;
               enabled = $w.Current.IsEnabled; buttons = @() }
    $buttons = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $buttonType)
    $count = 0
    foreach ($b in $buttons) {
        $count++
        if ($count -gt 400) { break }
        $id = $b.Current.AutomationId
        $name = $b.Current.Name
        if ($action -eq 'list' -and ($id -or $name)) {
            $info.buttons += @{ id = $id; name = $name; enabled = $b.Current.IsEnabled }
        }
        if ($action -eq 'invoke' -and $result.invoked -eq $null -and
            (($wanted -and $id -eq $wanted) -or ($wantedName -and $name -eq $wantedName))) {
            if (-not $b.Current.IsEnabled) {
                $result.invoked = @{ id = $id; name = $name; error = 'disabled' }
            } else {
                $pattern = $b.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
                $pattern.Invoke()
                $result.invoked = @{ id = $id; name = $name; window = $w.Current.Name }
            }
        }
    }
    $result.windows += $info
}
$result | ConvertTo-Json -Depth 6 -Compress
"""


def _powershell() -> str:
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    return os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")


class DesktopAutomationError(RuntimeError):
    """UI Automation could not complete the step; Desktop is left as it is."""


def _require_desktop_process(pid: int) -> None:
    """Refuse a pid that is gone or is not Power BI Desktop (a recycled pid)."""
    try:
        process_name = psutil.Process(pid).name()
    except psutil.Error as error:
        raise DesktopAutomationError("Power BI Desktop is not running") from error
    if process_name.casefold() != DESKTOP_PROCESS:
        raise DesktopAutomationError("That process is not Power BI Desktop")


def _as_list(value: Any) -> list[Any]:
    """ConvertTo-Json unwraps single-item arrays into the item itself."""
    if value is None:
        return []
    return [value] if isinstance(value, dict) else list(value)


def run(
    pid: int, action: str, automation_id: str = "", name: str = "", timeout: int = 60
) -> dict[str, Any]:
    if action not in _ACTIONS:
        raise ValueError(f"Unknown UI Automation action {action!r}")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise ValueError("A Desktop process id is required")
    _require_desktop_process(pid)
    encoded = base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii")
    env = {
        # Windows upper-cases the names in os.environ, so compare without regard to case.
        **{k: v for k, v in os.environ.items() if k.upper() != "PSEXECUTIONPOLICYPREFERENCE"},
        "STUDIO_UIA_PID": str(pid),
        "STUDIO_UIA_ACTION": action,
        "STUDIO_UIA_ID": automation_id,
        "STUDIO_UIA_NAME": name,
    }
    try:
        completed = subprocess.run(  # noqa: S603 - Windows PowerShell, fixed encoded script
            [
                _powershell(),
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise DesktopAutomationError("Power BI Desktop did not respond to UI Automation") from error
    except OSError as error:
        raise DesktopAutomationError("Windows PowerShell could not be started") from error
    output = completed.stdout.strip()
    if completed.returncode != 0 or not output:
        raise DesktopAutomationError(
            "UI Automation failed: " + (completed.stderr.strip()[-300:] or "no output")
        )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise DesktopAutomationError("UI Automation returned unreadable output") from error
    if not isinstance(payload, dict):
        raise DesktopAutomationError("UI Automation returned unreadable output")
    windows = _as_list(payload.get("windows"))
    for window in windows:
        window["buttons"] = _as_list(window.get("buttons"))
    payload["windows"] = windows
    if action == "dump":
        found = _as_list(payload.get("dump"))
        for item in found:
            item["buttons"] = _as_list(item.get("buttons"))
        payload["dump"] = found
    return dict(payload)


def windows(pid: int) -> list[dict[str, Any]]:
    """Top-level windows of the process with their buttons (name, AutomationId, enabled)."""
    return list(run(pid, "list")["windows"])


def dump(pid: int) -> list[dict[str, Any]]:
    """Every window, dialog and web-view root the process owns, with its buttons.

    Each item has type, name, id, className, enabled, topLevel and buttons (id, name,
    enabled). Slow (seconds): the contract script uses it, Studio's edit path does not.
    """
    return list(run(pid, "dump", timeout=120)["dump"])


def invoke_any(pid: int, automation_id: str = "", name: str = "") -> dict[str, Any]:
    """Like ``invoke`` but searches dialogs and web-view roots that are not top-level windows."""
    result = run(pid, "invoke_any", automation_id=automation_id, name=name, timeout=120)
    return _invoked(result, automation_id, name)


def invoke(pid: int, automation_id: str = "", name: str = "") -> dict[str, Any]:
    """Invoke the first enabled button with this AutomationId (or exact name)."""
    result = run(pid, "invoke", automation_id=automation_id, name=name)
    return _invoked(result, automation_id, name)


def _invoked(result: dict[str, Any], automation_id: str, name: str) -> dict[str, Any]:
    invoked = result.get("invoked")
    if not invoked:
        raise DesktopAutomationError(
            f"Power BI Desktop shows no {automation_id or name!r} button right now"
        )
    if invoked.get("error"):
        raise DesktopAutomationError(
            f"Power BI Desktop's {automation_id or name!r} button is {invoked['error']}"
        )
    return dict(invoked)
