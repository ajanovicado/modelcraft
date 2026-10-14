"""Read-only environment discovery and fail-closed capability gates.

The application can be inspected and exercised with its deterministic mock
composer on any supported Python host.  Real Power BI writes are a narrower
capability: they are enabled only on Windows, with the pinned tools present,
and while Power BI Desktop is closed.
"""

from __future__ import annotations

import importlib
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

from pydantic import Field

from ai_powerbi_analyst.contracts import ClosedModel

PINNED_TOOL_VERSIONS = {
    "pbi": "3.11.1",
    "powerbi-report-author": "0.1.4",
    "powerbi-desktop-bridge": "0.1.2",
}


class ToolProbe(ClosedModel):
    """Availability and best-effort version for one local executable."""

    name: str
    available: bool
    version: str | None = None
    distribution_version: str | None = None
    path: str | None = None


class EnvironmentReport(ClosedModel):
    """Facts collected without mutating the host."""

    operating_system: str
    operating_system_version: str | None = None
    python_version: str
    total_memory_gb: float = Field(ge=0)
    available_memory_gb: float = Field(ge=0)
    desktop_running: bool
    tools: dict[str, ToolProbe]


class Readiness(ClosedModel):
    """Capabilities derived from an :class:`EnvironmentReport`."""

    can_run_demo: bool
    can_write_real_powerbi: bool
    can_inspect_project: bool = True
    blockers: list[str] = Field(default_factory=list)


def _first_version_line(output: str) -> str | None:
    for line in output.splitlines():
        value = line.strip()
        if value:
            return value
    return None


def probe_tool(name: str, version_args: Iterable[str] = ("--version",)) -> ToolProbe:
    """Probe an executable using an argument vector and a short timeout."""

    bundled_distribution: str | None = None
    path: str | None
    if getattr(sys, "frozen", False) and name in {
        "pbi",
        "powerbi-report-author",
        "powerbi-desktop",
    }:
        from .bundled_tools import installed_tool

        try:
            bundled = installed_tool(name)
            path = str(bundled["path"])
            bundled_distribution = bundled.get("distribution_version")
        except (OSError, ValueError, TypeError, KeyError):
            # Installed builds never silently switch to a different developer CLI.
            return ToolProbe(name=name, available=False)
    else:
        path = shutil.which(name)
    if path is None and platform.system().casefold() == "windows":
        candidates: list[Path] = []
        user_profile = os.environ.get("USERPROFILE")
        local_app_data = os.environ.get("LOCALAPPDATA")
        roaming_app_data = os.environ.get("APPDATA")
        if user_profile:
            candidates.extend(
                (
                    Path(user_profile) / ".local" / "bin" / f"{name}.exe",
                    Path(user_profile) / ".local" / "bin" / name,
                )
            )
        if local_app_data and name.casefold() == "ollama":
            candidates.append(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe")
        if local_app_data:
            candidates.extend(
                (
                    Path(local_app_data) / "hermes" / "node" / f"{name}.cmd",
                    Path(local_app_data) / "hermes" / "node" / f"{name}.exe",
                )
            )
        if roaming_app_data:
            candidates.extend(
                (
                    Path(roaming_app_data) / "npm" / f"{name}.cmd",
                    Path(roaming_app_data) / "npm" / f"{name}.exe",
                )
            )
        located = next((candidate for candidate in candidates if candidate.is_file()), None)
        path = str(located) if located is not None else None
    if path is None:
        return ToolProbe(name=name, available=False)

    version: str | None = None
    try:
        result = subprocess.run(  # noqa: S603 - resolved local tool argv; shell=False; timeout-bounded
            [path, *version_args],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = _first_version_line(result.stdout) or _first_version_line(result.stderr)
    except (OSError, subprocess.SubprocessError):
        # Presence is still useful even when a third-party version command is
        # unavailable or behaves differently on a particular release.
        version = None
    return ToolProbe(
        name=name,
        available=True,
        version=version,
        path=path,
        distribution_version=bundled_distribution,
    )


def _parse_uv_tool_version(output: str, distribution: str) -> str | None:
    """Read an exact installed package version from ``uv tool list`` output."""

    pattern = re.compile(
        rf"^{re.escape(distribution)}\s+v(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(output)
    return match.group(1) if match else None


def _adjacent_distribution_version(executable: str, distribution: str) -> str | None:
    """Inspect the isolated tool environment without importing its code."""

    try:
        resolved = Path(executable).resolve(strict=True)
    except OSError:
        return None
    normalized = distribution.replace("-", "_")
    environment_root = resolved.parent.parent
    candidates = [
        *environment_root.glob(f"Lib/site-packages/{normalized}-*.dist-info/METADATA"),
        *environment_root.glob(f"lib/python*/site-packages/{normalized}-*.dist-info/METADATA"),
    ]
    for metadata in candidates:
        try:
            for line in metadata.read_text(encoding="utf-8").splitlines():
                if line.startswith("Version: "):
                    return line.removeprefix("Version: ").strip() or None
        except OSError:
            continue
    return None


def _uv_tool_distribution_version(distribution: str) -> str | None:
    uv = shutil.which("uv")
    if uv is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - resolved uv argv; shell=False; timeout-bounded
            # FORCE_COLOR in the parent environment would otherwise wrap the
            # distribution name in ANSI escapes and defeat the anchored parser.
            [uv, "tool", "list", "--color", "never"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_uv_tool_version(result.stdout, distribution)


def probe_pbi_cli() -> ToolProbe:
    """Probe pbi-cli while accounting for its stale 3.11.1 version banner.

    The 3.11.1 wheel reports package version 3.11.1 in distribution metadata,
    while ``pbi --version`` and ``pbi_cli.__version__`` still report 3.10.10.
    The release gate therefore records both and pins the installed distribution.
    """

    probe = probe_tool("pbi")
    if not probe.available or probe.path is None:
        return probe
    distribution_version = (
        probe.distribution_version
        or _adjacent_distribution_version(probe.path, "pbi-cli-tool")
        or _uv_tool_distribution_version("pbi-cli-tool")
    )
    return probe.model_copy(update={"distribution_version": distribution_version})


def probe_powerbi_desktop() -> ToolProbe:
    """Locate Desktop without launching it and read the Windows product version."""

    if platform.system().casefold() != "windows":
        return ToolProbe(name="powerbi-desktop", available=False)
    candidates: list[Path] = []
    try:
        import psutil

        for process in psutil.process_iter(["name", "exe"]):
            try:
                if (process.info.get("name") or "").casefold() == "pbidesktop.exe":
                    executable = process.info.get("exe")
                    if executable:
                        candidates.append(Path(executable))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except ImportError:  # pragma: no cover - psutil is a required runtime dependency
        pass
    discovered = shutil.which("PBIDesktop.exe")
    if discovered:
        candidates.append(Path(discovered))
    for variable in ("ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if not root:
            continue
        candidates.extend(
            (
                Path(root) / "Microsoft Power BI Desktop" / "bin" / "PBIDesktop.exe",
                Path(root) / "Microsoft" / "WindowsApps" / "PBIDesktop.exe",
            )
        )
    try:
        winreg = importlib.import_module("winreg")

        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    hive,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\PBIDesktop.exe",
                ) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                if isinstance(value, str) and value:
                    candidates.append(Path(value))
            except OSError:
                continue
    except ImportError:  # pragma: no cover - winreg exists on Windows
        pass

    powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if powershell is not None:
        try:
            result = subprocess.run(  # noqa: S603 - resolved powershell argv; shell=False; timeout-bounded
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$p=Get-AppxPackage -Name Microsoft.MicrosoftPowerBIDesktop "
                    "-ErrorAction SilentlyContinue | Select-Object -First 1; "
                    "if ($p) { Join-Path $p.InstallLocation 'bin\\PBIDesktop.exe' }",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
                shell=False,
            )
            appx_path = _first_version_line(result.stdout)
            if appx_path:
                candidates.append(Path(appx_path))
        except (OSError, subprocess.SubprocessError):
            pass

    executable = next((path for path in candidates if path.is_file()), None)
    if executable is None:
        return ToolProbe(name="powerbi-desktop", available=False)
    version: str | None = None
    if powershell is not None:
        environment = os.environ.copy()
        environment["AI_POWERBI_DESKTOP_EXE"] = str(executable)
        try:
            result = subprocess.run(  # noqa: S603 - resolved powershell argv; shell=False; timeout-bounded
                [
                    powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "(Get-Item -LiteralPath "
                    "$env:AI_POWERBI_DESKTOP_EXE).VersionInfo.ProductVersion",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
                env=environment,
                shell=False,
            )
            version = _first_version_line(result.stdout)
        except (OSError, subprocess.SubprocessError):
            version = None
    return ToolProbe(
        name="powerbi-desktop",
        available=True,
        version=version,
        path=str(executable),
    )


def _powerbi_desktop_running(process_names: Iterable[str] | None = None) -> bool:
    names = process_names
    if names is None:
        import psutil

        discovered: list[str] = []
        for process in psutil.process_iter(["name"]):
            try:
                discovered.append((process.info.get("name") or "").casefold())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        names = discovered
    normalized = {name.casefold() for name in names}
    return any(name in normalized for name in {"pbidesktop.exe", "pbidesktop"})


def assert_powerbi_desktop_closed() -> None:
    """Re-check the writer interlock immediately before every filesystem write."""

    if _powerbi_desktop_running():
        raise RuntimeError("Close Power BI Desktop before changing or restoring the report")


def collect_environment() -> EnvironmentReport:
    """Collect the current host facts used by the launcher and UI."""

    import psutil

    memory = psutil.virtual_memory()
    gib = 1024**3
    return EnvironmentReport(
        operating_system=platform.system(),
        operating_system_version=platform.version(),
        python_version=platform.python_version(),
        total_memory_gb=round(memory.total / gib, 2),
        available_memory_gb=round(memory.available / gib, 2),
        desktop_running=_powerbi_desktop_running(),
        tools={
            "pbi": probe_pbi_cli(),
            "powerbi-report-author": probe_tool("powerbi-report-author"),
            "powerbi-desktop-bridge": probe_tool("powerbi-desktop"),
            "ollama": probe_tool("ollama"),
            "node": probe_tool("node"),
            "powerbi-desktop": probe_powerbi_desktop(),
        },
    )


def evaluate_readiness(report: EnvironmentReport) -> Readiness:
    """Derive safe runtime capabilities from immutable environment facts."""

    blockers: list[str] = []
    ollama_probe = report.tools.get("ollama", ToolProbe(name="ollama", available=False))
    ollama_available = ollama_probe.available
    if not ollama_available:
        blockers.append("Ollama is not available")
    # Total and available memory stay in the report as information only: the
    # local Qwen 4B model runs whatever the RAM, so memory is never a blocker.

    is_windows = report.operating_system.casefold() == "windows"
    pbi_available = report.tools.get("pbi", ToolProbe(name="pbi", available=False)).available
    validator_available = report.tools.get(
        "powerbi-report-author",
        ToolProbe(name="powerbi-report-author", available=False),
    ).available
    bridge_available = report.tools.get(
        "powerbi-desktop-bridge",
        ToolProbe(name="powerbi-desktop-bridge", available=False),
    ).available
    node_available = report.tools.get("node", ToolProbe(name="node", available=False)).available
    desktop_available = report.tools.get(
        "powerbi-desktop",
        ToolProbe(name="powerbi-desktop", available=False),
    ).available

    if not is_windows:
        blockers.append("Real Power BI writes require Windows")
    if is_windows and not pbi_available:
        blockers.append("pbi-cli is not available")
    if is_windows and not validator_available:
        blockers.append("Microsoft Power BI report validator is not available")
    if is_windows and not bridge_available:
        blockers.append("Microsoft Power BI Desktop bridge is not available")
    if is_windows and not node_available:
        blockers.append("Node.js is not available")
    if is_windows and not desktop_available:
        blockers.append("Power BI Desktop is not installed or discoverable")
    if is_windows and report.desktop_running:
        blockers.append("Close Power BI Desktop")

    version_matches: dict[str, bool] = {}
    for tool_name, expected_version in PINNED_TOOL_VERSIONS.items():
        probe = report.tools.get(tool_name, ToolProbe(name=tool_name, available=False))
        version_text = probe.distribution_version or probe.version or ""
        discovered = re.findall(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", version_text)
        matches = probe.available and expected_version in discovered
        version_matches[tool_name] = matches
        if is_windows and probe.available and not matches:
            labels = {
                "pbi": "pbi-cli",
                "powerbi-report-author": "Microsoft validator",
                "powerbi-desktop-bridge": "Microsoft Desktop bridge",
            }
            label = labels[tool_name]
            blockers.append(f"{label} version {expected_version} is required")

    can_run_demo = "Ollama is not available" not in blockers
    can_write_real_powerbi = (
        is_windows
        and pbi_available
        and validator_available
        and bridge_available
        and node_available
        and desktop_available
        and not report.desktop_running
        and all(version_matches.values())
    )
    return Readiness(
        can_run_demo=can_run_demo,
        can_write_real_powerbi=can_write_real_powerbi,
        blockers=blockers,
    )
