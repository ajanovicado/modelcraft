"""Typed, shell-free adapters for the two PBIR command-line tools."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

JsonValue = dict[str, object] | list[object] | str | int | float | bool | None


class CommandFailed(RuntimeError):
    """A subprocess completed without satisfying its command contract."""


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    """Complete, serializable evidence from one argument-array invocation."""

    argv: tuple[str, ...]
    cwd: Path | None
    returncode: int
    stdout: str
    stderr: str
    parsed_json: JsonValue
    duration_ms: int

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0

    def require_success(self) -> CommandReceipt:
        if not self.succeeded:
            detail = (self.stderr or self.stdout).strip()
            if len(detail) > 500:
                detail = detail[:497] + "..."
            suffix = f": {detail}" if detail else ""
            raise CommandFailed(f"{self.argv[0]} failed with exit code {self.returncode}{suffix}")
        return self


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt: ...


class SubprocessRunner:
    """Run a literal argv sequence with no shell, interpolation, or stdin."""

    def __init__(self, *, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment) if environment is not None else None

    def run(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout: float = 30.0,
    ) -> CommandReceipt:
        if not argv or not all(isinstance(value, str) and value for value in argv):
            raise ValueError("argv must contain at least one non-empty string")
        if any("\x00" in value for value in argv):
            raise ValueError("argv cannot contain NUL bytes")
        if timeout <= 0:
            raise ValueError("timeout must be positive")

        started = time.monotonic()
        try:
            result = subprocess.run(  # noqa: S603 - validated argv; shell=False; stdin closed; timeout-bounded
                argv,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
                shell=False,
                env=self.environment,
            )
            returncode = result.returncode
            stdout = result.stdout
            stderr = result.stderr
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = _coerce_output(exc.stdout)
            existing = _coerce_output(exc.stderr)
            stderr = f"{existing}\ncommand timed out after {timeout:.1f}s".strip()
        except OSError as exc:
            returncode = 127
            stdout = ""
            stderr = str(exc)

        duration_ms = max(0, round((time.monotonic() - started) * 1000))
        return CommandReceipt(
            argv=argv,
            cwd=cwd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            parsed_json=_parse_json(stdout),
            duration_ms=duration_ms,
        )


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _parse_json(stdout: str) -> JsonValue:
    candidate = stdout.strip()
    if not candidate:
        return None
    try:
        parsed: JsonValue = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed


@dataclass(frozen=True, slots=True)
class ToolCapability:
    name: str
    executable: Path | None
    available: bool
    version: str | None = None
    probe_error: str | None = None


@dataclass(frozen=True, slots=True)
class PowerBICapabilities:
    pbi_cli: ToolCapability
    authoring_cli: ToolCapability
    platform_name: str
    official_schema_cache_verified: bool = False
    desktop_render_verified: bool = False

    @property
    def tooling_available(self) -> bool:
        return self.pbi_cli.available and self.authoring_cli.available

    @property
    def can_compose(self) -> bool:
        return self.tooling_available and self.official_schema_cache_verified

    @property
    def blockers(self) -> tuple[str, ...]:
        blockers: list[str] = []
        if not self.pbi_cli.available:
            blockers.append("pbi-cli is unavailable")
        if not self.authoring_cli.available:
            blockers.append("Microsoft report-authoring CLI is unavailable")
        if self.authoring_cli.available and not self.official_schema_cache_verified:
            blockers.append("Offline Microsoft schema availability is unverified")
        if self.platform_name.casefold() != "windows":
            blockers.append("Power BI Desktop render checks require Windows")
        if not self.desktop_render_verified:
            blockers.append("Power BI Desktop rendering has not been verified")
        return tuple(blockers)


Locator = Callable[[str], str | None]


def discover_capabilities(
    *,
    runner: CommandRunner | None = None,
    locator: Locator = shutil.which,
    platform_name: str | None = None,
    official_schema_cache_verified: bool = False,
) -> PowerBICapabilities:
    """Discover only installed global executables; never invoke npm or a network."""

    import platform

    command_runner = runner or SubprocessRunner()
    return PowerBICapabilities(
        pbi_cli=_probe_tool("pbi-cli", "pbi", locator, command_runner),
        authoring_cli=_probe_tool(
            "Microsoft report-authoring CLI",
            "powerbi-report-author",
            locator,
            command_runner,
        ),
        platform_name=platform_name or platform.system(),
        official_schema_cache_verified=official_schema_cache_verified,
        desktop_render_verified=False,
    )


def _probe_tool(
    display_name: str,
    executable_name: str,
    locator: Locator,
    runner: CommandRunner,
) -> ToolCapability:
    located = locator(executable_name)
    if located is None:
        return ToolCapability(name=display_name, executable=None, available=False)
    executable = Path(located)
    receipt = runner.run((str(executable), "--version"), timeout=10.0)
    if not receipt.succeeded:
        detail = (receipt.stderr or receipt.stdout).strip() or "version probe failed"
        return ToolCapability(
            name=display_name,
            executable=executable,
            available=False,
            probe_error=detail[:500],
        )
    output = f"{receipt.stdout}\n{receipt.stderr}"
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)", output)
    version = match.group(1) if match else output.strip() or None
    return ToolCapability(
        name=display_name,
        executable=executable,
        available=True,
        version=version,
    )


class PbiCliAdapter:
    """Narrow pbi-cli report-layer interface used by deterministic recipes."""

    def __init__(self, executable: Path, runner: CommandRunner | None = None) -> None:
        self.executable = executable
        self.runner = runner or SubprocessRunner()

    def _report_args(self, report_path: Path, *, write: bool) -> list[str]:
        args = [
            str(self.executable),
            "--json",
            "report",
            "--path",
            str(report_path.resolve()),
        ]
        if write:
            args.append("--no-sync")
        return args

    def _visual_args(self, report_path: Path, *, write: bool) -> list[str]:
        args = [
            str(self.executable),
            "--json",
            "visual",
            "--path",
            str(report_path.resolve()),
        ]
        if write:
            args.append("--no-sync")
        return args

    def validate(self, report_path: Path, *, full: bool = True) -> CommandReceipt:
        args = self._report_args(report_path, write=False) + ["validate"]
        if full:
            args.append("--full")
        return self.runner.run(tuple(args), timeout=60.0)

    def list_pages(self, report_path: Path) -> CommandReceipt:
        args = self._report_args(report_path, write=False) + ["list-pages"]
        return self.runner.run(tuple(args)).require_success()

    def add_page(
        self,
        report_path: Path,
        *,
        page_id: str,
        display_name: str,
        width: int = 1280,
        height: int = 720,
    ) -> CommandReceipt:
        args = self._report_args(report_path, write=True) + [
            "add-page",
            "--display-name",
            display_name,
            "--name",
            page_id,
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        return self.runner.run(tuple(args)).require_success()

    def delete_page(self, report_path: Path, *, page_id: str) -> CommandReceipt:
        args = self._report_args(report_path, write=True) + ["delete-page", page_id]
        return self.runner.run(tuple(args)).require_success()

    def set_theme(self, report_path: Path, *, theme_path: Path) -> CommandReceipt:
        args = self._report_args(report_path, write=True) + [
            "set-theme",
            "--file",
            str(theme_path.resolve()),
        ]
        return self.runner.run(tuple(args)).require_success()

    def delete_visual(
        self,
        report_path: Path,
        *,
        page_id: str,
        visual_id: str,
    ) -> CommandReceipt:
        args = self._visual_args(report_path, write=True) + [
            "delete",
            visual_id,
            "--page",
            page_id,
        ]
        return self.runner.run(tuple(args)).require_success()

    def add_visual(
        self,
        report_path: Path,
        *,
        page_id: str,
        visual_id: str,
        visual_type: str,
        x: int,
        y: int,
        width: int,
        height: int,
    ) -> CommandReceipt:
        args = self._visual_args(report_path, write=True) + [
            "add",
            "--page",
            page_id,
            "--type",
            visual_type,
            "--name",
            visual_id,
            "--x",
            str(x),
            "--y",
            str(y),
            "--width",
            str(width),
            "--height",
            str(height),
        ]
        return self.runner.run(tuple(args)).require_success()

    def bind_visual(
        self,
        report_path: Path,
        *,
        page_id: str,
        visual_id: str,
        bindings: Sequence[tuple[str, str]],
    ) -> CommandReceipt:
        allowed_roles = {
            "category",
            "value",
            "row",
            "field",
            "legend",
            "indicator",
            "goal",
        }
        args = self._visual_args(report_path, write=True) + ["bind", visual_id]
        for role, reference in bindings:
            if role not in allowed_roles:
                raise ValueError(f"unsupported pbi-cli binding role: {role}")
            args.extend((f"--{role}", reference))
        args.extend(("--page", page_id))
        return self.runner.run(tuple(args)).require_success()

    def set_visual_container(
        self,
        report_path: Path,
        *,
        page_id: str,
        visual_id: str,
        title: str,
    ) -> CommandReceipt:
        args = self._visual_args(report_path, write=True) + [
            "set-container",
            visual_id,
            "--page",
            page_id,
            "--border-show",
            "true",
            "--background-show",
            "true",
            "--title",
            title,
        ]
        return self.runner.run(tuple(args)).require_success()


class AuthoringCliAdapter:
    """Read-only adapter for Microsoft's independent PBIR validator."""

    def __init__(self, executable: Path, runner: CommandRunner | None = None) -> None:
        self.executable = executable
        self.runner = runner or SubprocessRunner()

    def validate(self, report_path: Path) -> CommandReceipt:
        return self.runner.run(
            (str(self.executable), "validate", str(report_path.resolve())),
            timeout=90.0,
        )
