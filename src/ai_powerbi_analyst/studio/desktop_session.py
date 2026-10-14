"""Power BI Desktop as Studio's tool for model edits and full-fidelity previews.

Contract measured on Desktop 2.157.1354.0 (2026-09-28, scratchpad editexp/contract.py):
- Bridge `open` connects in about 20 s. The pid it prints can belong to another
  instance, so Studio identifies its own by a new PBIDesktop pid whose currentFilePath
  is the file it opened, and re-checks that pid's create time before every step.
- hasUnsavedChanges is false at connect and turns true about 10 s later for a
  Studio-written file, as Desktop normalizes it. A UI Automation Save right after opening
  clears it (5.5 s). From then on the flag means real changes.
- TOM writes (pbi measure create/update) set the flag. The UIA Save persists them:
  PBIXRay read the new measure back, with every column cardinality unchanged.
- The "Save changes?" prompt is an embedded web view UI Automation cannot reach. So
  Studio closes Desktop only when nothing is unsaved, never kills it, and rolls back a
  failed model edit with the inverse TOM write, never by discarding.
- Desktop locks the open PBIX, so Studio touches the file only after the process exits.

Rules this module enforces (design review P1, P2, P4, P9, P28):
- Identity. A Desktop instance is (pid, psutil create time, currentFilePath), re-verified
  before every save, close, screenshot and model edit. The pid the bridge's ``open``
  prints is never used. Two instances holding the same file are refused, not guessed at.
- Nothing here can kill a process. ``close`` posts WM_CLOSE and waits; if Desktop asks
  "Save changes?" it is left exactly as it is and the caller is told. No code path in
  these two modules ends a process by force (a test greps their source for one).
- The "Save changes?" prompt is an embedded web view. UI Automation found only the disabled
  main window after WM_CLOSE (2026-09-28); whether its "Don't save" button can be reached
  was never established. The contract script (evals/imported_edit/desktop_contract.py)
  probes that; until a receipt says otherwise Studio never closes a Desktop that is dirty.
- Launching. Desktop is started through a hidden ``cmd /c start`` that exits at once, with
  CREATE_BREAKAWAY_FROM_JOB when the sidecar's job allows it, so it is neither a child of
  the sidecar nor in its kill-on-close job. The bridge's own ``open`` (a plain detached
  spawn) stays as the fallback. Survival is proven live by the contract script only.
- Model changes beyond measures (columns, tables, relationships, hierarchies, field parameters,
  calculation groups, roles, Power Query, model settings) go through Studio's TOM runner
  (``desktop_model``, ``scripts/studio_tom_runner.py``) on the engine port of the verified
  instance, in one save with its own rollback. Measure-only batches keep using ``pbi measure``.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

import psutil

from . import desktop_model, desktop_uia
from .report_edits import CreateMeasure, DeleteMeasure, RenameMeasure, UpdateMeasure

DESKTOP_PROCESS = desktop_uia.DESKTOP_PROCESS
# psutil create times are floats that round-trip through JSON and the OS clock; a second
# separates one process from another that reuses its pid.
CREATE_TIME_TOLERANCE = 1.0

# Windows process creation flags (numeric so tests can assert them on any platform).
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_JOB_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000

# A prompt that is not answered within this long after WM_CLOSE is reported, not waited out.
_PROMPT_POLL_SECONDS = 2.0
_BLOCKING_LABEL = "a dialog in Power BI Desktop"
_VERSION_IN_PATH = re.compile(r"Microsoft\.MicrosoftPowerBIDesktop_(\d+(?:\.\d+){1,3})_", re.I)
_SAFE_VERSION = re.compile(r"[0-9A-Za-z._-]{1,40}")


# Seconds without any sign of a save before Save is pressed again, and the most presses.
_SAVE_RETRY_SECONDS = 12.0
_SAVE_PRESSES = 4


class DesktopSessionError(RuntimeError):
    """Power BI Desktop could not complete a step; nothing was discarded."""


class UnsavedDesktopWork(DesktopSessionError):
    """The report open in Power BI Desktop has changes Studio did not make."""


class DesktopDialogOpen(DesktopSessionError):
    """A dialog is open in Power BI Desktop, so it cannot be driven until the user finishes it.

    ``dialog`` is its title (or "a dialog in Power BI Desktop" when Desktop only says that its
    main window is disabled). Callers read it instead of parsing the message.
    """

    def __init__(self, message: str, dialog: str) -> None:
        super().__init__(message)
        self.dialog = dialog


class _OutcomeUnknown(DesktopSessionError):
    """A model change was sent but Desktop never answered, so it may have been applied."""


class DesktopRefreshFailed(DesktopSessionError):
    """Power BI could not reload the data a change needed (a source it cannot reach, typically),
    and the change was undone in Desktop. ``reason`` is Power BI's own message."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class DesktopInstance:
    """One Desktop process on one file. Equality is the identity Studio persists."""

    pid: int
    create_time: float
    path: Path

    def to_json(self) -> dict[str, Any]:
        return {"pid": self.pid, "create_time": self.create_time, "path": str(self.path)}

    @classmethod
    def from_json(cls, data: Mapping[str, Any] | str) -> DesktopInstance:
        """Rebuild an identity stored by ``to_json``; ValueError when it is not one."""
        try:
            value = json.loads(data) if isinstance(data, str) else data
            pid, created, path = value["pid"], value["create_time"], value["path"]
        except (TypeError, KeyError, ValueError) as error:
            raise ValueError("Not a saved Power BI Desktop identity") from error
        if (
            isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or isinstance(created, bool)
            or not isinstance(created, int | float)
            or not math.isfinite(created)
            or created <= 0
            or not isinstance(path, str)
            or not path
        ):
            raise ValueError("Not a saved Power BI Desktop identity")
        return cls(pid, float(created), Path(path))


@dataclass(frozen=True)
class SaveResult:
    """What a Desktop save left on disk. ``changed`` is False when the flag was clear and
    the file stayed byte for byte the same (a save with nothing to write)."""

    path: Path
    sha256: str
    seconds: float
    changed: bool = True


@dataclass(frozen=True)
class DesktopReadiness:
    """Whether model edits may run: every tool present and a passing contract receipt for
    the installed Desktop version. Report-only edits never wait on this."""

    bridge_ok: bool
    pbi_ok: bool
    desktop_ok: bool
    contract_passed: bool
    version: str | None
    reasons: list[str] = field(default_factory=list)
    # Studio's TOM runner (pbi-cli's pinned Python and libraries): needed for model changes other
    # than measures. None in ``runner_reason`` when it is ready.
    runner_ok: bool = True
    runner_reason: str | None = None

    @property
    def can_edit_model(self) -> bool:
        return self.bridge_ok and self.pbi_ok and self.desktop_ok and self.contract_passed

    @property
    def can_edit_model_objects(self) -> bool:
        """Columns, tables, relationships and the rest of the model, not only measures."""
        return self.can_edit_model and self.runner_ok


def _norm_path(path: str | Path) -> str | None:
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return None


def _same_path(a: str | Path, b: str | Path) -> bool:
    left, right = _norm_path(a), _norm_path(b)
    return left is not None and left == right


def launch_argv(desktop_exe: str, path: str | Path) -> list[str]:
    """The argv that starts Desktop on ``path`` through an intermediate that exits at once.

    ``start`` returns as soon as Desktop is running and cmd exits, so Desktop's parent is a
    dead process: a tree-wide kill of the sidecar's children no longer reaches it. Combined
    with ``launch_flags(breakaway=True)`` it is also outside the sidecar's job object.

    These are the logical pieces, for inspection only. Do not run them through
    ``subprocess.list2cmdline``: it quotes only what contains a space, so ``&`` or ``^`` in a
    folder name would reach cmd bare and split the command. ``launch_command_line`` is what
    ``launch`` runs.
    """
    return ["cmd.exe", "/c", "start", "", "/B", str(desktop_exe), str(path)]


def launch_command_line(desktop_exe: str, path: str | Path) -> str:
    """The exact command line ``launch`` hands to CreateProcess: every part quoted for cmd.

    Inside quotes cmd treats ``& ^ | < > ( ) , ; =`` literally, so only ``%`` (expanded even
    inside quotes), ``"`` and line breaks need refusing (``_cmd_safe``). The text after /c
    starts with ``start``, not a quote, so cmd does not strip any quote pair.
    """
    return f'cmd.exe /c start "" /B "{desktop_exe}" "{path}"'


def launch_flags(*, breakaway: bool) -> int:
    """Creation flags for the intermediate cmd: hidden, and optionally out of the job."""
    return CREATE_NO_WINDOW | (CREATE_BREAKAWAY_FROM_JOB if breakaway else 0)


def _cmd_safe(path: str | Path) -> bool:
    """cmd expands %VAR% (and !VAR! when delayed expansion is on) even inside quotes and
    treats a quote as syntax."""
    return not any(character in str(path) for character in '%!"\r\n')


def job_limit_flags() -> int | None:
    """LimitFlags of the job object this process belongs to; None when it is in none.

    Unreadable (not Windows, or the query fails) counts as "in a job that does not allow
    breakaway": the caller then takes the route that does not need it.
    """
    try:
        # A private WinDLL: argtypes set here must not leak into other users of kernel32.
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        in_job = wintypes.BOOL()
        if not kernel32.IsProcessInJob(kernel32.GetCurrentProcess(), None, ctypes.byref(in_job)):
            return 0
        if not in_job.value:
            return None
        buffer = (ctypes.c_ubyte * 256)()
        size = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
        # JobObjectExtendedLimitInformation = 9; LimitFlags sits after two LARGE_INTEGERs.
        if not kernel32.QueryInformationJobObject(None, 9, buffer, size, None):
            return 0
        return int(ctypes.c_uint32.from_buffer(buffer, 16).value)
    except (AttributeError, OSError):
        return 0


def breakaway_permitted() -> bool:
    """True when a child may leave this process's job (or there is no job)."""
    flags = job_limit_flags()
    if flags is None:
        return True
    return bool(flags & (_JOB_LIMIT_BREAKAWAY_OK | _JOB_LIMIT_SILENT_BREAKAWAY_OK))


def version_from_path(desktop_exe: str | Path) -> str | None:
    """The version in a Store install folder name (``Microsoft.MicrosoftPowerBIDesktop_<v>_``)."""
    found = _VERSION_IN_PATH.search(str(desktop_exe))
    return found.group(1) if found else None


def _file_version(path: Path) -> str | None:
    """The four-part file version of an executable, read with the Win32 version API."""
    try:
        version = ctypes.WinDLL("version", use_last_error=True)
        version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
        version.GetFileVersionInfoSizeW.restype = wintypes.DWORD
        version.GetFileVersionInfoW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        version.VerQueryValueW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wintypes.UINT),
        ]
        size = version.GetFileVersionInfoSizeW(str(path), None)
        if not size:
            return None
        data = ctypes.create_string_buffer(size)
        if not version.GetFileVersionInfoW(str(path), 0, size, data):
            return None
        block = ctypes.c_void_p()
        length = wintypes.UINT()
        if not version.VerQueryValueW(data, "\\", ctypes.byref(block), ctypes.byref(length)):
            return None
        if length.value < 52 or not block.value:
            return None
        # VS_FIXEDFILEINFO: signature, struct version, then dwFileVersionMS/LS.
        fields = (ctypes.c_uint32 * 4).from_address(block.value)
        if fields[0] != 0xFEEF04BD:
            return None
        ms, ls = fields[2], fields[3]
        return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    except (AttributeError, OSError, ValueError):
        return None


def default_receipts_dir() -> Path:
    """Where the contract script writes its receipts in a source checkout."""
    return Path(__file__).resolve().parents[3] / "evals" / "imported_edit" / "evidence"


def read_receipt(version: str | None, receipts_dir: Path | None = None) -> dict[str, Any] | None:
    """The contract receipt for exactly this Desktop version, or None."""
    if not version or not _SAFE_VERSION.fullmatch(version):
        return None
    folder = receipts_dir if receipts_dir is not None else default_receipts_dir()
    try:
        receipt = json.loads((folder / f"desktop-{version}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or receipt.get("desktop_version") != version:
        return None
    return receipt


def read_model_probe_receipt(
    version: str | None, receipts_dir: Path | None = None
) -> dict[str, Any] | None:
    """The model probe receipt (``evals/imported_edit/model_probe.py``) for exactly this Desktop
    version, else None. Its ``verified_ops`` name the kinds of model change outside Microsoft's
    documented list that passed in Desktop: the kind's own step, the save and read-back of the
    file, and the undo (the probe lists nothing when either of the last two failed)."""
    if not version or not _SAFE_VERSION.fullmatch(version):
        return None
    folder = receipts_dir if receipts_dir is not None else default_receipts_dir()
    try:
        text = (folder / f"model-probe-{version}.json").read_text(encoding="utf-8")
        receipt = json.loads(text)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(receipt, dict)
        or receipt.get("kind") != "model_probe"
        or receipt.get("desktop_version") != version
    ):
        return None
    return receipt


def _pbidesktop_pids() -> set[int]:
    found: set[int] = set()
    for process in psutil.process_iter(["name"]):
        if (process.info["name"] or "").casefold() == DESKTOP_PROCESS:
            found.add(process.pid)
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


_UNEXPECTED_EDIT = "Power BI Desktop could not apply the change"


def unsupported_measure_change(operations: list[Any]) -> str | None:
    """Why the port cannot apply these measure operations, in words a user can act on; None
    when it can apply them all.

    Hiding or showing a measure is one such change: ``pbi measure update`` has no flag for it
    (only ``measure create`` has ``--hidden``; checked against the pinned tool's help), and
    deleting and re-creating the measure to set the flag would drop what the tool does not read
    back (annotations, KPI, detail rows) and break its dependents for a moment. A silent no-op
    would report a change that is never made, so it is refused, before Desktop is touched.
    """
    for op in operations:
        if isinstance(op, UpdateMeasure) and op.hidden is not None:
            return (
                "Studio cannot hide or show a measure yet. In Power BI Desktop, right-click "
                f"[{op.name}] in the Data pane and choose {'Hide' if op.hidden else 'Unhide'}"
            )
    return None


_DUPLICATE_MESSAGE = (
    "Power BI Desktop has this report open in more than one window. Close the extra "
    "window, then try again."
)


class _Holder(NamedTuple):
    pid: int
    create_time: float
    connected: bool


def _key(instance: DesktopInstance) -> tuple[int, float]:
    return instance.pid, instance.create_time


def _looks_like_path(part: str) -> bool:
    return os.sep in part or "/" in part or part.lower().endswith((".exe", ".js", ".mjs", ".cjs"))


def _exists(program: str) -> bool:
    return bool(program) and (Path(program).is_file() or shutil.which(program) is not None)


def _blocking_titles(windows: list[dict[str, Any]]) -> list[str]:
    """What in a UI Automation window list blocks Save and close (see ``blocking_windows``).

    The main window is the one with the ribbon's Save button; it stays in the list, disabled,
    while a modal is up. With no such window, a lone window is taken as the main one and
    several are all unknown, which counts as blocking (refusing is the safe direction).
    """
    main = next(
        (w for w in windows if any(b.get("id") == "save" for b in w.get("buttons", []))), None
    )
    if main is None and len(windows) == 1:
        main = windows[0]
    titles: list[str] = []
    for window in windows:
        title = str(window.get("name") or "").strip()
        if window is main:
            if not window.get("enabled", True):
                titles.append(_BLOCKING_LABEL)
        else:
            titles.append(title or "a dialog")
    return titles


class DesktopController:
    """Open, save, query and close one PBIX in Power BI Desktop, without keystrokes."""

    def __init__(
        self,
        bridge: list[str],
        pbi: str,
        desktop_exe: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        receipts_dir: Path | None = None,
        model_runner: desktop_model.Runner | None = None,
        runner_script: Path | None = None,
    ) -> None:
        self.bridge, self.pbi, self.desktop_exe = bridge, pbi, desktop_exe
        self._clock, self._sleep = clock, sleep
        self._receipts_dir = receipts_dir
        # The TOM runner: injected by tests, else found and pinned next to ``pbi`` on first use.
        self._model_runner = model_runner
        self._runner_script = runner_script
        self._normalized: set[tuple[int, float]] = set()
        self._version: str | None = None
        # The launch route of the most recent ``launch``: "breakaway", "cmd" or "bridge".
        self.last_launch_route: str | None = None

    # --- bridge -----------------------------------------------------------------------

    def _bridge(self, *args: str, timeout: int = 60) -> dict[str, Any]:
        env = {**os.environ, "PBI_DESKTOP_PATH": self.desktop_exe}
        try:
            completed = subprocess.run(  # noqa: S603 - pinned bridge, argv only
                [*self.bridge, *args],
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
            raise DesktopSessionError("Power BI Desktop did not answer in time") from error
        except OSError as error:
            raise DesktopSessionError("The Power BI Desktop bridge could not be started") from error
        text = completed.stdout.strip()
        if not text.startswith("{"):
            raise DesktopSessionError(
                "The Power BI Desktop bridge failed: "
                + (completed.stderr.strip()[-300:] or text[-300:])
            )
        try:
            payload: dict[str, Any] = json.loads(text)
        except json.JSONDecodeError as error:
            # A reply cut short (the bridge was killed, or Desktop closed mid-answer).
            raise DesktopSessionError(
                "The Power BI Desktop bridge gave an unreadable answer"
            ) from error
        return payload

    def _instances(self) -> list[dict[str, Any]]:
        found = self._bridge("status", timeout=30).get("instances", [])
        return [i for i in found if isinstance(i, dict)] if isinstance(found, list) else []

    def manifest(self, instance: DesktopInstance) -> dict[str, Any]:
        """The bridge's manifest of the verified instance (the contract script hashes it)."""
        self.status(instance)
        return self._bridge("manifest", "--pid", str(instance.pid), timeout=60)

    # --- identity -----------------------------------------------------------------------

    @staticmethod
    def _on_path(item: dict[str, Any], path: Path) -> bool:
        current = item.get("currentFilePath")
        return isinstance(current, str) and bool(current) and _same_path(current, path)

    def _identity(self, pid: object) -> float | None:
        """The create time of a live Desktop process, None for anything else (a dead pid,
        another program that reuses it, a process Studio may not inspect)."""
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        try:
            process = psutil.Process(pid)
            if process.name().casefold() != DESKTOP_PROCESS:
                return None
            return float(process.create_time())
        except psutil.Error:
            return None

    def _holders(self, path: Path) -> list[_Holder]:
        """Live Desktop processes the bridge lists with ``path`` open, connected or not."""
        found: dict[int, _Holder] = {}
        for item in self._instances():
            if not self._on_path(item, path):
                continue
            created = self._identity(item.get("pid"))
            pid = item.get("pid")
            if created is not None and isinstance(pid, int):
                found[pid] = _Holder(pid, created, item.get("bridgeStatus") == "connected")
        return list(found.values())

    def status(self, instance: DesktopInstance) -> dict[str, Any]:
        """The bridge's entry for ``instance`` after re-verifying who it is.

        The pid must still be the same PBIDesktop process (create time within a second)
        with the same file open, and no other window may hold that file: with two, Studio
        refuses instead of guessing which one to save or close.
        """
        # The bridge drops an instance from its list for a moment while Desktop is busy
        # (seen right after load), so a missing entry is retried before it counts.
        for attempt in range(6):
            self._check_alive(instance)
            items = [i for i in self._instances() if self._on_path(i, instance.path)]
            mine = [i for i in items if i.get("pid") == instance.pid]
            others = [
                i
                for i in items
                if i.get("pid") != instance.pid and self._identity(i.get("pid")) is not None
            ]
            if others:
                raise DesktopSessionError(_DUPLICATE_MESSAGE)
            if mine:
                return mine[0]
            if attempt < 5:
                self._sleep(1)
        raise DesktopSessionError("Power BI Desktop no longer has this report open")

    def _check_alive(self, instance: DesktopInstance) -> None:
        try:
            process = psutil.Process(instance.pid)
            if (
                process.name().casefold() != DESKTOP_PROCESS
                or abs(process.create_time() - instance.create_time) > CREATE_TIME_TOLERANCE
            ):
                raise DesktopSessionError("Power BI Desktop was restarted outside Studio")
        except psutil.NoSuchProcess as error:
            raise DesktopSessionError("Power BI Desktop was closed") from error
        except psutil.Error as error:
            raise DesktopSessionError("Studio cannot check Power BI Desktop's process") from error

    def adopt(
        self,
        identity: DesktopInstance | Mapping[str, Any] | str,
        *,
        normalized: bool = False,
    ) -> DesktopInstance | None:
        """Re-attach to an instance saved before a sidecar restart.

        Returns it only while pid, create time (within a second) and the file it has open
        all still match and the bridge sees it connected; otherwise None, and the caller
        treats the report as not open. ``normalized`` restores what the caller stored about
        ``open``'s normalization (the flag is not part of the identity).
        """
        try:
            saved = (
                identity
                if isinstance(identity, DesktopInstance)
                else DesktopInstance.from_json(identity)
            )
        except ValueError:
            return None
        created = self._identity(saved.pid)
        if created is None or abs(created - saved.create_time) > CREATE_TIME_TOLERANCE:
            return None
        holders = self._holders(saved.path)
        if len(holders) > 1:
            raise DesktopSessionError(_DUPLICATE_MESSAGE)
        if len(holders) != 1 or holders[0].pid != saved.pid or not holders[0].connected:
            return None
        if normalized:
            self._normalized.add(_key(saved))
        return saved

    def is_normalized(self, instance: DesktopInstance) -> bool:
        """True after ``open`` saved Desktop's own change to a file Studio wrote.

        From then on a set unsaved-changes flag means the user did something; before it the
        flag is ambiguous, because Desktop dirties a Studio-written file ~10 s after load.
        """
        return _key(instance) in self._normalized

    def mark_normalized(self, instance: DesktopInstance) -> None:
        self._normalized.add(_key(instance))

    # --- lifecycle --------------------------------------------------------------------

    def find(self, path: Path) -> DesktopInstance | None:
        """The connected Desktop instance that has ``path`` open, if any.

        Only an instance on exactly that file counts; a Desktop the user has open on some
        other report is never returned. Two windows on the file raise DesktopSessionError.
        """
        holders = self._holders(Path(path))
        if len(holders) > 1:
            raise DesktopSessionError(_DUPLICATE_MESSAGE)
        if holders and holders[0].connected:
            return DesktopInstance(holders[0].pid, holders[0].create_time, Path(path))
        return None

    def launch_argv(self, path: Path) -> list[str]:
        """The command ``launch`` runs for ``path`` (see the module-level ``launch_argv``)."""
        return launch_argv(self.desktop_exe, path)

    def launch_route(self) -> str:
        """Which route ``launch`` takes here: "breakaway" (the hidden ``cmd start`` leaves the
        sidecar's job object as well) or "cmd" (the job does not allow breakaway)."""
        return "breakaway" if breakaway_permitted() else "cmd"

    def launch_survives_parent(self) -> bool:
        """True when a Desktop started by ``launch`` should outlive the sidecar being killed.

        Both routes start Desktop through an intermediate that exits at once, so it is not a
        child of the sidecar and a tree-wide kill of the sidecar (the 2026-09-27 dev restart)
        misses it. Only "breakaway" also leaves the sidecar's kill-on-close job, so
        only it also survives the job being closed, which is what the packaged app's host
        does on exit. Actual survival cannot be proven without Desktop: the contract
        script's launch-survival step does it live.
        """
        return self.launch_route() == "breakaway"

    def launch(self, path: Path) -> str:
        """Start Desktop on ``path`` outside the sidecar's process tree; returns the route.

        Does not wait for the bridge. Raises DesktopSessionError when no direct route could
        start it (``open`` then falls back to the bridge's own launch).
        """
        path = Path(path)
        if not _cmd_safe(path) or not _cmd_safe(self.desktop_exe):
            raise DesktopSessionError("The report's file name cannot be opened directly")
        routes = [("cmd", False)]
        if breakaway_permitted():
            routes.insert(0, ("breakaway", True))
        before = _pbidesktop_pids()
        for route, breakaway in routes:
            try:
                process = subprocess.Popen(  # noqa: S603 - fixed cmd line, every part quoted
                    launch_command_line(self.desktop_exe, path),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=launch_flags(breakaway=breakaway),
                )
            except OSError:
                # A job that forbids breakaway refuses CREATE_BREAKAWAY_FROM_JOB (access denied).
                continue
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=20)  # cmd returns as soon as Desktop has been started
            if process.returncode not in (0, None) and not (_pbidesktop_pids() - before):
                # Only a start that produced no Desktop is retried: a second route after a
                # nonzero cmd exit that did start one would open a second window.
                continue
            self.last_launch_route = route
            return route
        raise DesktopSessionError("Power BI Desktop could not be started")

    def _wait_for_new(
        self,
        path: Path,
        before: set[int],
        timeout: float,
        *,
        give_up_if_nothing_after: float | None = None,
    ) -> DesktopInstance | None:
        """The new (not in ``before``) connected instance on ``path``, or None on timeout.

        None also comes back early when ``give_up_if_nothing_after`` seconds pass without any
        new Desktop process at all, so the caller can try another way to launch.
        """
        started = self._clock()
        deadline = started + timeout
        while self._clock() < deadline:
            try:
                items = self._instances()
            except DesktopSessionError:
                items = []  # the bridge answers late while Desktop starts; keep waiting
            matches = [
                i
                for i in items
                if self._on_path(i, path)
                and isinstance(i.get("pid"), int)
                and i["pid"] not in before
                and self._identity(i["pid"]) is not None
            ]
            if len({i["pid"] for i in matches}) > 1:
                raise DesktopSessionError(_DUPLICATE_MESSAGE)
            for item in matches:
                created = self._identity(item["pid"])
                if item.get("bridgeStatus") == "connected" and created is not None:
                    return DesktopInstance(item["pid"], created, path)
            if (
                give_up_if_nothing_after is not None
                and self._clock() - started >= give_up_if_nothing_after
                and not (_pbidesktop_pids() - before)
            ):
                return None
            self._sleep(1)
        return None

    def open(self, path: Path, *, timeout: float = 180, normalize: bool = True) -> DesktopInstance:
        """Open ``path`` in Desktop (or reuse the instance that has it) and make its
        unsaved-changes flag meaningful by saving Desktop's own normalization.

        The instance is the new PBIDesktop process that connects with ``path`` open, never
        the pid the bridge's ``open`` prints (that is whichever instance was connected).
        """
        path = Path(path)
        holders = self._holders(path)
        if len(holders) > 1:
            raise DesktopSessionError(_DUPLICATE_MESSAGE)
        if holders and holders[0].connected:
            return DesktopInstance(holders[0].pid, holders[0].create_time, path)
        instance: DesktopInstance | None
        if holders:
            # Desktop has the file open but the bridge is not connected yet (loading): wait
            # for it, because a second window on the same file would only be refused.
            instance = self._wait_for_new(path, set(), timeout)
        else:
            before = _pbidesktop_pids()
            try:
                self.launch(path)
            except DesktopSessionError:
                instance = None
            else:
                instance = self._wait_for_new(path, before, timeout, give_up_if_nothing_after=45)
            if instance is None and not (_pbidesktop_pids() - before):
                # Nothing was started (or nothing appeared): let the bridge launch it.
                self.last_launch_route = "bridge"
                self._bridge(
                    "open", str(path), "--timeout", str(int(timeout)), timeout=int(timeout) + 30
                )
                instance = self._wait_for_new(path, before, timeout)
        if instance is None:
            raise DesktopSessionError("Power BI Desktop did not open the report in time")
        if normalize:
            # Desktop flags a Studio-written file as changed shortly after load; wait
            # for that, then save so the flag afterwards means real edits.
            settle = self._clock() + 25
            while True:
                dirty = bool(self.status(instance).get("hasUnsavedChanges"))
                if dirty or self._clock() >= settle:
                    break
                self._sleep(1)
            if dirty:
                self.save(instance)
                self._normalized.add(_key(instance))
        return instance

    # --- dialogs, save and close ---------------------------------------------------------

    def blocking_windows(self, instance: DesktopInstance) -> list[str]:
        """Titles of what stops Desktop from answering: a disabled main window (a modal
        dialog is up), or any top-level window besides the main one. [] when nothing does.

        Measured 2026-09-28: after WM_CLOSE on a dirty report the main window reported
        enabled=False and the only other trace of the prompt was inside a web view. So a
        disabled main window is reported as "a dialog in Power BI Desktop"; a real second
        window is reported by its title.
        """
        self._check_alive(instance)
        try:
            windows = desktop_uia.windows(instance.pid)
        except desktop_uia.DesktopAutomationError as error:
            raise DesktopSessionError(
                "Studio could not check whether Power BI Desktop is showing a dialog"
            ) from error
        return _blocking_titles(windows)

    def _blocking_windows(self, instance: DesktopInstance) -> list[str]:
        return self.blocking_windows(instance)

    def _refuse_if_blocked(self, instance: DesktopInstance) -> None:
        blocking = self.blocking_windows(instance)
        if blocking:
            raise DesktopDialogOpen(
                "Power BI Desktop is showing a dialog (" + ", ".join(blocking[:2]) + "). "
                "Finish it in Power BI Desktop, then try again.",
                blocking[0],
            )

    def _verify_saved(self, path: Path) -> str | None:
        """None when the saved file is a healthy package, else what is wrong with it."""
        from .pbix_package import PbixPackage  # deferred: the package layer is heavier

        try:
            with zipfile.ZipFile(path) as archive:
                bad = archive.testzip()
            if bad is not None:
                return f"part {bad} is damaged"
            PbixPackage(path)
        except (zipfile.BadZipFile, OSError, ValueError) as error:
            return str(error) or type(error).__name__
        return None

    @staticmethod
    def _stamp(path: Path) -> tuple[int, int]:
        info = path.stat()
        return info.st_mtime_ns, info.st_size

    def save(self, instance: DesktopInstance, *, timeout: float = 90) -> SaveResult:
        """Invoke Desktop's own Save and wait until the file is written, clean and readable.

        Done means all of: the unsaved-changes flag is false, the file's hash changed, the
        archive tests clean and Studio's package reader opens it. A save with nothing to
        write leaves the file identical with the flag false, and returns ``changed=False``.
        """
        state = self.status(instance)
        self._refuse_if_blocked(instance)
        path = instance.path
        try:
            before_hash, before_stamp = _sha256(path), self._stamp(path)
        except OSError as error:
            raise DesktopSessionError("Studio cannot read the report Desktop has open") from error
        dirty_before = bool(state.get("hasUnsavedChanges"))
        started = self._clock()
        try:
            desktop_uia.invoke(instance.pid, automation_id="save")
        except desktop_uia.DesktopAutomationError as error:
            if not dirty_before:
                return SaveResult(path, before_hash, self._clock() - started, False)
            raise DesktopSessionError(
                "Studio could not press Save in Power BI Desktop. Save the report there "
                "yourself, then try again."
            ) from error
        deadline = started + timeout
        # The file is written and the flag cleared within a few seconds of each other (a
        # save took 3 to 5.5 s in all). With nothing dirty a save changes nothing, so a
        # short wait proves it; with changes the file is given longer to appear.
        no_change_after = 8.0 if dirty_before else 3.0
        clean_since: float | None = None
        next_dialog_check = self._clock() + 3.0
        # A Save pressed while Desktop is still loading the report is ignored without a trace
        # (contract run of 2026-09-29: the flag stayed set and the file untouched for 90 s,
        # while the same button saved in 2.7 s once the report had loaded). And a Save made
        # while it loads can be followed by Desktop marking the report changed again (the
        # untouched original: file written 30 s after launch, flag set again). So while the
        # report stays unsaved the button is pressed again after a quiet spell, a few times.
        next_press = started + _SAVE_RETRY_SECONDS
        presses = 1
        problem: str | None = None
        while self._clock() < deadline:
            self._sleep(0.5)
            now = self._clock()
            try:
                stamp = self._stamp(path)
            except OSError:
                continue  # replaced under us for a moment
            if self.status(instance).get("hasUnsavedChanges"):
                clean_since = None
                if now >= next_press and presses < _SAVE_PRESSES:
                    with contextlib.suppress(desktop_uia.DesktopAutomationError):
                        desktop_uia.invoke(instance.pid, automation_id="save")
                    presses += 1
                    next_press = now + _SAVE_RETRY_SECONDS
            elif stamp != before_stamp:
                problem = self._verify_saved(path)
                if problem is None:
                    try:
                        after_hash = _sha256(path)
                    except OSError:
                        continue
                    return SaveResult(path, after_hash, now - started, after_hash != before_hash)
            else:
                clean_since = now if clean_since is None else clean_since
                if now - clean_since >= no_change_after:
                    return SaveResult(path, before_hash, now - started, False)
            if now >= next_dialog_check:
                next_dialog_check = now + 3.0
                try:
                    blocked = self.blocking_windows(instance)
                except DesktopSessionError:
                    blocked = []
                if blocked:
                    raise DesktopDialogOpen(
                        "Power BI Desktop opened a dialog while saving. Finish it in Power BI "
                        "Desktop; Studio did not change anything.",
                        blocked[0],
                    )
        if problem is not None:
            raise DesktopSessionError(
                f"Power BI Desktop saved the report, but the file failed Studio's check ({problem})"
            )
        raise DesktopSessionError("Power BI Desktop did not finish saving in time")

    def close(self, instance: DesktopInstance, *, timeout: float = 60) -> None:
        """Close Desktop when nothing is unsaved and no dialog is open. Never kills it.

        The window gets WM_CLOSE, as the title bar's X does. If Desktop then asks "Save
        changes?" (the main window goes disabled) the prompt is reported and left alone:
        answering it is the user's decision, and its buttons live in a web view that UI
        Automation was never shown to reach.
        """
        state = self.status(instance)
        self._refuse_if_blocked(instance)
        if state.get("hasUnsavedChanges"):
            raise UnsavedDesktopWork(
                "Power BI Desktop has unsaved changes to this report. Save or discard them "
                "in Power BI Desktop, then try again."
            )
        try:
            process = psutil.Process(instance.pid)
        except psutil.NoSuchProcess:
            return
        if _post_close(instance.pid) == 0:
            raise DesktopSessionError(
                "Studio could not find Power BI Desktop's window to close it. Close it "
                "yourself, then try again."
            )
        waited = 0.0
        while waited < timeout:
            step = min(_PROMPT_POLL_SECONDS, timeout - waited)
            try:
                process.wait(timeout=step)
            except psutil.NoSuchProcess:
                break
            except psutil.TimeoutExpired:
                waited += step
                try:
                    prompt = self.blocking_windows(instance)
                except DesktopSessionError:
                    continue  # cannot tell; the next wait shows whether it exited
                if prompt:
                    raise DesktopDialogOpen(
                        "Power BI Desktop is asking a question before it closes ("
                        + ", ".join(prompt[:2])
                        + "). Answer it in Power BI Desktop; Studio left it open.",
                        prompt[0],
                    ) from None
            else:
                break
        else:
            raise DesktopSessionError(
                "Power BI Desktop did not close. Close it yourself, then try again."
            )
        self._normalized.discard(_key(instance))

    def screenshot(self, instance: DesktopInstance, page_id: str, output: Path) -> Path:
        self.status(instance)
        self._bridge(
            "screenshot",
            page_id,
            "--pid",
            str(instance.pid),
            "--output",
            str(output),
            "--scale",
            "1",
            "--wait-seconds",
            "20",
            timeout=90,
        )
        if not output.is_file() or output.stat().st_size < 100:
            raise DesktopSessionError("Power BI Desktop did not capture the page")
        return output

    # --- version and readiness ------------------------------------------------------------

    def version(self) -> str | None:
        """Desktop's product version (from PBIDesktop.exe, else the Store folder name)."""
        if self._version:
            return self._version
        exe = Path(self.desktop_exe)
        if not exe.is_file():
            return None
        self._version = _file_version(exe) or version_from_path(exe)
        return self._version

    def readiness(self) -> DesktopReadiness:
        """Whether model edits can run. Cheap: file checks and one receipt read, no process.

        Desktop updates monthly from the Store and cannot be pinned, so a model edit needs a
        passing contract receipt for the *installed* version; report-only edits do not.
        """
        bridge_ok = bool(self.bridge) and all(
            _exists(part) for part in self.bridge if _looks_like_path(part)
        )
        pbi_ok = _exists(self.pbi)
        desktop_ok = Path(self.desktop_exe).is_file()
        version = self.version() if desktop_ok else None
        receipt = read_receipt(version, self._receipts_dir)
        passed = bool(receipt and receipt.get("passed") is True)
        runner_problem = self.model_runner_problem() if pbi_ok else None
        reasons: list[str] = []
        if not bridge_ok:
            reasons.append("The Power BI Desktop bridge is not installed")
        if not pbi_ok:
            reasons.append("The Power BI command-line tool is not installed")
        if not desktop_ok:
            reasons.append("Power BI Desktop is not installed")
        elif version is None:
            reasons.append("Studio could not read Power BI Desktop's version")
        elif receipt is None:
            reasons.append(f"Power BI Desktop {version} has not been checked with Studio yet")
        elif not passed:
            reasons.append(f"Power BI Desktop {version} failed Studio's compatibility check")
        return DesktopReadiness(
            bridge_ok,
            pbi_ok,
            desktop_ok,
            passed,
            version,
            reasons,
            runner_ok=pbi_ok and runner_problem is None,
            runner_reason=runner_problem
            if pbi_ok
            else "The Power BI command-line tool is not installed",
        )

    def model_runner_problem(self) -> str | None:
        """Why Studio's TOM runner cannot run here (in words for the user), None when it can.
        Checks the pinned environment's files and hashes; starts no process."""
        if self._model_runner is not None:
            return None
        try:
            desktop_model.runner_environment(self.pbi, script=self._runner_script)
        except desktop_model.ModelRunnerUnavailable as error:
            return str(error)
        return None

    def _runner(self) -> desktop_model.Runner:
        if self._model_runner is not None:
            return self._model_runner
        try:
            environment = desktop_model.runner_environment(self.pbi, script=self._runner_script)
        except desktop_model.ModelRunnerUnavailable as error:
            raise DesktopSessionError(str(error)) from error
        return desktop_model.subprocess_runner(environment)

    # --- model edits over TOM ------------------------------------------------------------

    def _engine_port(self, instance: DesktopInstance) -> str:
        """The msmdsrv port of the verified instance's own engine (a child of that process)."""
        self.status(instance)
        try:
            engines = [
                p
                for p in psutil.Process(instance.pid).children()
                if p.name().casefold() == "msmdsrv.exe"
            ]
            if len(engines) != 1:
                raise DesktopSessionError("Power BI Desktop's model engine is still loading")
            args = engines[0].cmdline()
            port_file = Path(args[args.index("-s") + 1]) / "msmdsrv.port.txt"
            port = port_file.read_text(encoding="utf-16-le").strip("\x00\r\n ﻿")
        except (psutil.Error, OSError, ValueError, IndexError) as error:
            raise DesktopSessionError("Power BI Desktop's model engine is not ready") from error
        if not port.isdecimal():
            raise DesktopSessionError("Power BI Desktop's model engine port is unreadable")
        return port

    def _connection(self, instance: DesktopInstance) -> str:
        port = self._engine_port(instance)
        name = f"studio-edit-{instance.pid}"
        self._pbi("connect", "--data-source", f"localhost:{port}", "--name", name, connection=None)
        return name

    def _pbi(self, *args: str, connection: str | None, stdin: str | None = None) -> dict[str, Any]:
        argv = [self.pbi, "--json", *(("-c", connection) if connection else ()), *args]
        try:
            completed = subprocess.run(  # noqa: S603 - pinned pbi-cli, argv only
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            # Whether the change reached the model is unknown: callers that undo must try.
            raise _OutcomeUnknown("Power BI Desktop did not answer the change in time") from error
        except OSError as error:
            # A tool that cannot be run or a pipe that broke mid-call: same unknown outcome, and
            # in either case a DesktopSessionError so the port's own rollback runs.
            raise _OutcomeUnknown(
                "Studio could not run Power BI's command-line tool for this change"
            ) from error
        text = completed.stdout.strip()
        try:
            payload: dict[str, Any] = json.loads(text) if text else {}
        except json.JSONDecodeError:
            payload = {}
        if completed.returncode != 0 or "error" in payload:
            message = payload.get("error") or completed.stderr.strip()[-400:] or text[-400:]
            raise DesktopSessionError(f"Power BI rejected the change: {message}")
        return payload

    def _probe(self, connection: str, measure: str) -> None:
        query = 'EVALUATE ROW("value", [' + measure.replace("]", "]]") + "])"
        file = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w", suffix=".dax", delete=False, encoding="utf-8"
            ) as handle:
                file = handle.name
                handle.write(query)
        except OSError as error:
            # The change is already in the open model, so this must reach the rollback as a
            # session error, not as a raw disk error.
            if file:
                with contextlib.suppress(OSError):
                    Path(file).unlink(missing_ok=True)
            raise DesktopSessionError(
                "Studio could not write the temporary file that checks the measure works"
            ) from error
        try:
            self._pbi(
                "dax",
                "execute",
                "--file",
                file,
                "--max-rows",
                "1",
                "--timeout",
                "30",
                connection=connection,
            )
        finally:
            Path(file).unlink(missing_ok=True)

    def apply_measure_operations(
        self, instance: DesktopInstance, operations: list[Any], tables: dict[str, str]
    ) -> list[str]:
        """Apply measure operations live in Desktop, probing each; undo them all if one
        fails. ``tables`` maps measure names to their home table. Nothing is saved here.

        A rename is followed by the ``UpdateMeasure`` of each measure whose formula used the old
        name (the edit layer appends them). Until its own update runs, such a measure is broken
        in the open model, and so is every measure that uses it, so probing an update right
        after it would fail on a formula that the next update fixes: the probes of updates that
        come after a rename wait until every operation has been applied.
        """
        refusal = unsupported_measure_change(operations)
        if refusal is not None:
            raise DesktopSessionError(refusal)
        connection = self._connection(instance)
        # Each undo step is a pbi argv tail plus the expression it reads from stdin.
        undo: list[tuple[tuple[str, ...], str | None]] = []
        # The undo of the change being sent right now: used only when Desktop never answered
        # and the change may or may not have landed.
        pending: tuple[tuple[str, ...], str | None] | None = None
        tables_before = dict(tables)
        done: list[str] = []
        renamed = False
        later: list[str] = []  # measures to probe once everything is applied
        try:
            for op in operations:
                pending = None
                if isinstance(op, CreateMeasure):
                    args = ["measure", "create", op.name, "-t", op.table, "-e", "-"]
                    if op.format_string:
                        args += ["--format-string", op.format_string]
                    if op.folder:
                        args += ["--folder", op.folder]
                    if op.description:
                        args += ["--description", op.description]
                    pending = (("measure", "delete", op.name, "-t", op.table), None)
                    self._pbi(*args, connection=connection, stdin=op.expression)
                    undo.append(pending)
                    self._probe(connection, op.name)
                    tables[op.name] = op.table
                    done.append(f"created [{op.name}]")
                elif isinstance(op, UpdateMeasure):
                    table = self._home_table(tables, op.name)
                    before = self._pbi(
                        "measure", "get", op.name, "-t", table, connection=connection
                    )
                    args = ["measure", "update", op.name, "-t", table]
                    stdin = None
                    if op.expression is not None:
                        args += ["-e", "-"]
                        stdin = op.expression
                    if op.format_string is not None:
                        args += ["--format-string", op.format_string]
                    if op.folder is not None:
                        args += ["--folder", op.folder]
                    if op.description is not None:
                        args += ["--description", op.description]
                    pending = self._restore_args(op.name, table, before)
                    self._pbi(*args, connection=connection, stdin=stdin)
                    undo.append(pending)
                    if renamed:
                        later.append(op.name)
                    else:
                        self._probe(connection, op.name)
                    done.append(f"updated [{op.name}]")
                elif isinstance(op, RenameMeasure):
                    table = self._home_table(tables, op.name)
                    pending = (("measure", "rename", op.new_name, op.name, "-t", table), None)
                    self._pbi(
                        "measure",
                        "rename",
                        op.name,
                        op.new_name,
                        "-t",
                        table,
                        connection=connection,
                    )
                    undo.append(pending)
                    tables[op.new_name] = tables.pop(op.name)
                    later = [op.new_name if name == op.name else name for name in later]
                    renamed = True
                    self._probe(connection, op.new_name)
                    done.append(f"renamed [{op.name}] to [{op.new_name}]")
                elif isinstance(op, DeleteMeasure):
                    table = self._home_table(tables, op.name)
                    before = self._pbi(
                        "measure", "get", op.name, "-t", table, connection=connection
                    )
                    pending = self._recreate_args(op.name, table, before)
                    self._pbi("measure", "delete", op.name, "-t", table, connection=connection)
                    undo.append(pending)
                    tables.pop(op.name)
                    later = [name for name in later if name != op.name]
                    done.append(f"deleted [{op.name}]")
            pending = None
            for name in dict.fromkeys(later):
                self._probe(connection, name)
        except Exception as error:
            # Any failure (a timeout, an unreadable answer, a bug) leaves the earlier
            # operations in the open model, and Studio never discards Desktop's work, so
            # they are undone the same way they were made.
            tables.clear()
            tables.update(tables_before)
            steps = list(undo)
            # The change in flight, when Desktop never answered it and it is not recorded yet;
            # its undo failing is expected if the change never landed, so it is not reported.
            speculative = (
                pending
                if isinstance(error, _OutcomeUnknown)
                and pending is not None
                and not any(pending is step for step in undo)
                else None
            )
            if speculative is not None:
                steps.append(speculative)
            failed = []
            for step in reversed(steps):
                undo_args, stdin = step
                try:
                    self._pbi(*undo_args, connection=connection, stdin=stdin)
                except Exception:  # noqa: BLE001 - one failed undo must not stop the others
                    if step is not speculative:
                        failed.append(undo_args[2])
            message = str(error) if isinstance(error, DesktopSessionError) else _UNEXPECTED_EDIT
            if failed:
                message += (
                    " Studio could not undo the change to "
                    + ", ".join(f"[{name}]" for name in dict.fromkeys(failed))
                    + " in Power BI Desktop. Do not save the report: close it and choose "
                    "Don't save, then try again."
                )
            if isinstance(error, DesktopSessionError) and not failed:
                raise
            raise DesktopSessionError(message) from error
        return done

    def apply_model_operations(
        self, instance: DesktopInstance, operations: list[Any]
    ) -> desktop_model.ModelApplyResult:
        """Apply any model operations (measures included) live in Desktop through Studio's TOM
        runner, in one save: checked by Desktop's own error states and test queries, and undone
        in Desktop if anything fails. Nothing is saved to the file here.

        Raises DesktopRefreshFailed when a data refresh the batch needs failed (the change was
        undone), and DesktopSessionError otherwise; its message says not to save the report when
        Studio could not undo what reached the model."""
        refusal = unsupported_measure_change(operations)
        if refusal is not None:
            raise DesktopSessionError(refusal)
        server = f"localhost:{self._engine_port(instance)}"
        runner = self._runner()
        try:
            return desktop_model.apply_model_operations(operations, server=server, runner=runner)
        except desktop_model.ModelRefreshFailed as error:
            raise DesktopRefreshFailed(str(error), error.reason) from error
        except desktop_model.ModelChangeFailed as error:
            message = str(error)
            if not error.rolled_back:
                message += (
                    ". Close the report in Power BI Desktop and choose Don't save, then try again."
                )
            if isinstance(error, desktop_model.ModelOutcomeUnknown):
                raise _OutcomeUnknown(message) from error
            raise DesktopSessionError(message) from error
        except desktop_model.ModelChangeError as error:
            raise DesktopSessionError(str(error)) from error

    def model_snapshot(self, instance: DesktopInstance) -> dict[str, Any]:
        """The live model's metadata as the runner reads it (read only)."""
        server = f"localhost:{self._engine_port(instance)}"
        try:
            return desktop_model.snapshot(self._runner(), server)
        except desktop_model.ModelChangeError as error:
            raise DesktopSessionError(str(error)) from error

    def model_query(
        self, instance: DesktopInstance, queries: Mapping[str, str], *, role: str | None = None
    ) -> dict[str, Any]:
        """Read-only DAX queries (EVALUATE) against the live model: id -> {ok, rows | error}.
        With ``role`` the model is seen as that role sees it (Desktop's "View as")."""
        server = f"localhost:{self._engine_port(instance)}"
        try:
            return desktop_model.query(self._runner(), server, queries, role=role)
        except desktop_model.ModelChangeError as error:
            raise DesktopSessionError(str(error)) from error

    def query_model(self, instance: DesktopInstance, queries: Mapping[str, str]) -> dict[str, Any]:
        """``desktop_model.query`` over the model Desktop has open (id -> {ok, rows | error}),
        the shape ``report_tests.QueryEvaluator`` takes; read only, as the report's own user."""
        return self.model_query(instance, queries)

    def model_probe_coverage(self) -> tuple[str | None, frozenset[str]]:
        """(the installed Desktop version, the kinds of model change outside Microsoft's
        documented list that a passing model probe receipt for exactly that version covers).
        Cheap: one file read, no process."""
        version = self.version() if Path(self.desktop_exe).is_file() else None
        receipt = read_model_probe_receipt(version, self._receipts_dir)
        if receipt is None:
            return version, frozenset()
        verified = receipt.get("verified_ops")
        if not isinstance(verified, list):
            return version, frozenset()
        return version, frozenset(str(key) for key in verified if isinstance(key, str))

    def undo_model_operations(
        self, instance: DesktopInstance, applied: list[desktop_model.ModelApplyResult]
    ) -> None:
        """Put the live model back from the before-images of batches ``apply_model_operations``
        applied (newest first). Nothing is saved here."""
        server = f"localhost:{self._engine_port(instance)}"
        try:
            desktop_model.undo_applied(applied, server=server, runner=self._runner())
        except desktop_model.ModelChangeError as error:
            raise DesktopSessionError(str(error)) from error

    @staticmethod
    def _home_table(tables: dict[str, str], name: str) -> str:
        try:
            return tables[name]
        except KeyError:
            raise DesktopSessionError(
                f"Studio does not know which table the measure [{name}] belongs to"
            ) from None

    @staticmethod
    def _restore_args(name: str, table: str, before: dict[str, Any]) -> tuple[tuple[str, ...], str]:
        """Put a measure back as ``pbi measure get`` read it before the update."""
        args = ["measure", "update", name, "-t", table, "-e", "-"]
        args += ["--format-string", str(before.get("formatString") or "")]
        args += ["--folder", str(before.get("displayFolder") or "")]
        args += ["--description", str(before.get("description") or "")]
        return tuple(args), str(before.get("expression", ""))

    @staticmethod
    def _recreate_args(
        name: str, table: str, before: dict[str, Any]
    ) -> tuple[tuple[str, ...], str]:
        """Create a deleted measure again from its before-image."""
        args = ["measure", "create", name, "-t", table, "-e", "-"]
        if before.get("formatString"):
            args += ["--format-string", str(before["formatString"])]
        if before.get("displayFolder"):
            args += ["--folder", str(before["displayFolder"])]
        if before.get("description"):
            args += ["--description", str(before["description"])]
        return tuple(args), str(before.get("expression", ""))


def post_close(pid: int) -> int:
    """WM_CLOSE to one Desktop process's windows, without ``close``'s checks.

    For the contract script, which must provoke the "Save changes?" prompt on a scratch
    instance it launched. Studio's own code uses ``DesktopController.close``.
    """
    return _post_close(pid)


def _post_close(pid: int) -> int:
    """Post WM_CLOSE to the process's visible top-level windows (what the title-bar X does)."""
    user32 = ctypes.windll.user32
    handles: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd: int, _: int) -> bool:
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4):
            handles.append(hwnd)
        return True

    user32.EnumWindows(callback_type(callback), 0)
    for hwnd in handles:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return len(handles)
