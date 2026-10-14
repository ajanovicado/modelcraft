"""Release Ollama memory, verify it is gone, then open the validated PBIP."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from filelock import FileLock

from ai_powerbi_analyst.integrity import sha256_file
from ai_powerbi_analyst.preflight import (
    _powerbi_desktop_running,
    assert_powerbi_desktop_closed,
)


class CommandResult(Protocol):
    returncode: int
    stdout: str


def _post_json(url: str, payload: dict[str, object]) -> None:
    request = urllib.request.Request(  # noqa: S310 - loopback URL is validated
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=8):  # noqa: S310 - loopback URL is validated
        return


def _run(argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed local tool argv; shell=False
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        shell=False,
    )


def _launch_desktop(executable: Path, pbip_path: Path) -> None:
    subprocess.Popen(  # noqa: S603 - both paths are frozen local release artifacts
        (str(executable), str(pbip_path)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        close_fds=True,
    )


def _powerbi_desktop_executable_running(executable: Path) -> bool:
    """Confirm a running Desktop process came from the exact attested executable."""

    import psutil

    expected = os.path.normcase(os.path.abspath(executable))
    for process in psutil.process_iter(["name", "exe"]):
        try:
            process_path = process.info.get("exe")
            if process_path and os.path.normcase(os.path.abspath(process_path)) == expected:
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


class PowerBIDesktopLauncher:
    def __init__(
        self,
        pbip_path: str | Path,
        *,
        model: str,
        ollama_endpoint: str = "http://127.0.0.1:11434",
        platform_name: str | None = None,
        ollama_executable: str | Path = "ollama",
        ollama_executable_hash: str | None = None,
        desktop_executable: str | Path | None = None,
        desktop_executable_hash: str | None = None,
        http_post: Callable[[str, dict[str, object]], None] = _post_json,
        run_command: Callable[[tuple[str, ...]], CommandResult] = _run,
        launch_desktop: Callable[[Path, Path], None] = _launch_desktop,
        open_path: Callable[[Path], None] | None = None,
        pre_open_check: Callable[[], None] | None = None,
        operation_lock_path: str | Path | None = None,
        desktop_running_check: Callable[[], bool] = _powerbi_desktop_running,
        desktop_identity_check: Callable[[Path], bool] | None = None,
        confirmation_timeout_seconds: float = 90.0,
        lock_timeout_seconds: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.pbip_path = Path(pbip_path).resolve()
        self.model = model
        self.ollama_endpoint = ollama_endpoint.rstrip("/")
        self.platform_name = platform_name or platform.system()
        self.ollama_executable = Path(ollama_executable)
        self.ollama_executable_hash = ollama_executable_hash
        self.desktop_executable = (
            Path(desktop_executable).resolve(strict=False)
            if desktop_executable is not None
            else None
        )
        self.desktop_executable_hash = desktop_executable_hash
        self.http_post = http_post
        self.run_command = run_command
        self.launch_desktop = launch_desktop
        self.open_path = open_path
        self.pre_open_check = pre_open_check
        self.operation_lock_path = (
            Path(operation_lock_path).resolve(strict=False)
            if operation_lock_path is not None
            else None
        )
        self.reveal_marker_path = (
            self.operation_lock_path.parent / ".reveal-in-progress.json"
            if self.operation_lock_path is not None
            else None
        )
        self.desktop_running_check = desktop_running_check
        self.desktop_identity_check = desktop_identity_check or _powerbi_desktop_executable_running
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.lock_timeout_seconds = lock_timeout_seconds
        self.sleep = sleep

    def __call__(self) -> Mapping[str, Any]:
        if self.platform_name.casefold() != "windows":
            return {"opened": False, "reason": "Power BI Desktop reveal requires Windows."}
        if not self.pbip_path.is_file() or self.pbip_path.suffix.casefold() != ".pbip":
            return {"opened": False, "reason": "The prepared PBIP project file is missing."}
        if self.desktop_executable is None or not self.desktop_executable.is_file():
            return {
                "opened": False,
                "reason": "The attested Power BI Desktop executable is missing.",
            }
        launch_issued = False
        try:
            lock = (
                FileLock(str(self.operation_lock_path), timeout=self.lock_timeout_seconds)
                if self.operation_lock_path is not None
                else nullcontext()
            )
            # Hold the same inter-process lock used by compose/reset from the
            # last integrity check until Desktop is observed.  This closes the
            # reveal/write race between two browser sessions.
            with lock:
                if self.pre_open_check is not None:
                    self.pre_open_check()
                _require_executable_hash(
                    self.ollama_executable,
                    self.ollama_executable_hash,
                    "Ollama",
                )
                _require_executable_hash(
                    self.desktop_executable,
                    self.desktop_executable_hash,
                    "Power BI Desktop",
                )
                if self.desktop_running_check():
                    raise RuntimeError("Power BI Desktop is already running")
                if self.reveal_marker_path is not None:
                    if self.reveal_marker_path.exists():
                        raise RuntimeError(
                            "A prior Power BI reveal is unresolved; use the clean demo reset "
                            "before writing or revealing again"
                        )
                    _write_reveal_marker(self.reveal_marker_path, self.pbip_path)
                self.http_post(
                    f"{self.ollama_endpoint}/api/generate",
                    {"model": self.model, "keep_alive": 0},
                )
                ollama = str(self.ollama_executable)
                running = self.run_command((ollama, "ps"))
                if running.returncode != 0:
                    raise RuntimeError("could not verify Ollama memory release")
                resident_models = _resident_model_names(running.stdout)
                if self.model.casefold() in {name.casefold() for name in resident_models}:
                    stopped = self.run_command((ollama, "stop", self.model))
                    if stopped.returncode != 0:
                        raise RuntimeError("Ollama refused to unload the selected model")
                verified = self.run_command((ollama, "ps"))
                if verified.returncode != 0:
                    raise RuntimeError("could not verify final Ollama memory release")
                remaining_models = _resident_model_names(verified.stdout)
                if remaining_models:
                    names = ", ".join(remaining_models)
                    raise RuntimeError(
                        f"Ollama still has resident model memory ({names}); Desktop was not opened"
                    )
                _require_executable_hash(
                    self.desktop_executable,
                    self.desktop_executable_hash,
                    "Power BI Desktop",
                )
                if self.open_path is not None:
                    self.open_path(self.pbip_path)
                else:
                    self.launch_desktop(self.desktop_executable, self.pbip_path)
                launch_issued = True
                deadline = time.monotonic() + self.confirmation_timeout_seconds
                while not self.desktop_identity_check(self.desktop_executable):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Power BI Desktop did not start within the confirmation window; "
                            "writes remain blocked until clean demo reset"
                        )
                    self.sleep(0.25)
                if self.reveal_marker_path is not None:
                    self.reveal_marker_path.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - desktop boundary fails closed
            if not launch_issued and self.reveal_marker_path is not None:
                self.reveal_marker_path.unlink(missing_ok=True)
            return {"opened": False, "reason": str(exc)}
        return {
            "opened": True,
            "path": str(self.pbip_path),
            "desktop_executable": str(self.desktop_executable),
        }


def _require_executable_hash(path: Path, expected_hash: str | None, label: str) -> None:
    if expected_hash is None:
        return
    if not path.is_file() or sha256_file(path) != expected_hash:
        raise RuntimeError(f"{label} executable identity changed after attestation")


def _resident_model_names(stdout: str) -> tuple[str, ...]:
    """Parse the first NAME column from the stable human-readable `ollama ps`."""

    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ()
    if lines[0].casefold() in {"no models running", "no running models"}:
        return ()
    if lines[0].split(maxsplit=1)[0].casefold() == "name":
        lines = lines[1:]
    return tuple(line.split(maxsplit=1)[0] for line in lines if line.split())


def _write_reveal_marker(path: Path, pbip_path: Path) -> None:
    payload = {
        "schema_version": "1.0.0",
        "status": "launching",
        "pbip_path": str(pbip_path),
        "started_at": datetime.now(UTC).isoformat(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def assert_powerbi_write_allowed(reveal_marker_path: str | Path) -> None:
    """Block writes while Desktop is open or a ShellExecute launch is unresolved."""

    assert_powerbi_desktop_closed()
    marker = Path(reveal_marker_path)
    if marker.exists():
        raise RuntimeError(
            "Power BI reveal is still in progress or unresolved; run the clean demo reset"
        )
