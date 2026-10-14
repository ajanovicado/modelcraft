"""Hydrate and render-check a committed PBIP without rewriting its definitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

import psutil


class DesktopHydrationError(RuntimeError):
    """The opened Desktop report could not be proven populated and renderable."""


class CommandResult(Protocol):
    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[tuple[str, ...], float, Mapping[str, str] | None], CommandResult]


@dataclass(frozen=True, slots=True)
class ProjectSnapshot:
    """Immutable pre-launch bytes and resolved PBIP artifact paths."""

    project_root: Path
    pbip_path: Path
    report_path: Path
    semantic_model_path: Path
    page_ids: tuple[str, ...]
    referenced_tables: tuple[str, ...]
    files: tuple[tuple[str, bytes], ...]
    ignored_local_paths: frozenset[str]


@dataclass(frozen=True, slots=True)
class DesktopHydrationResult:
    """Evidence returned only after all refresh and render checks pass."""

    data_refreshed: bool
    render_verified: bool
    cache_saved: bool
    refreshed_tables: tuple[str, ...]
    screenshot_paths: tuple[str, ...]
    evidence_path: str
    desktop_pid: int
    referenced_table_rows: tuple[tuple[str, int], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "data_refreshed": self.data_refreshed,
            "render_verified": self.render_verified,
            "cache_saved": self.cache_saved,
            "refreshed_tables": list(self.refreshed_tables),
            "screenshot_paths": list(self.screenshot_paths),
            "evidence_path": self.evidence_path,
            "desktop_pid": self.desktop_pid,
            "referenced_table_rows": dict(self.referenced_table_rows),
        }


def _run_command(
    argv: tuple[str, ...],
    timeout_seconds: float,
    environment: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - pinned tool argv; shell=False
        argv,
        check=False,
        capture_output=True,
        text=True,
        # The Node-based Power BI CLIs emit UTF-8 JSON, independently of the
        # Windows ANSI code page. Locale decoding corrupts Unicode file paths.
        encoding="utf-8",
        timeout=timeout_seconds,
        shell=False,
        env=environment,
    )


class DesktopHydrationVerifier:
    """Refresh an opened PBIP and prove its real Desktop render is populated.

    This intentionally does not save the report. Current Desktop versions can
    normalize PBIP/TMDL definitions on save, and a cache produced from those
    normalized definitions is not necessarily compatible with the committed
    source bytes. Hydrating on every launch keeps the validated source immutable.
    """

    def __init__(
        self,
        *,
        pbi_executable: str | Path,
        bridge_executable: str | Path,
        desktop_executable: str | Path,
        run_command: CommandRunner = _run_command,
        command_timeout_seconds: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.pbi_executable = str(Path(pbi_executable).resolve(strict=False))
        self.bridge_executable = str(Path(bridge_executable).resolve(strict=False))
        self.desktop_executable = str(Path(desktop_executable).resolve(strict=False))
        self.run_command = run_command
        self.command_timeout_seconds = command_timeout_seconds
        self.sleep = sleep

    def capture_baseline(self, pbip_path: str | Path) -> ProjectSnapshot:
        """Capture every governed project byte before Desktop starts."""

        pbip = Path(pbip_path).expanduser().resolve(strict=True)
        if pbip.suffix.casefold() != ".pbip" or not pbip.is_file():
            raise DesktopHydrationError("Committed PBIP entry point is missing")
        project_root = pbip.parent
        report = _resolve_report(pbip)
        semantic_model = _resolve_semantic_model(report, project_root)
        page_ids = _load_page_ids(report)
        referenced_tables = _load_referenced_tables(report)
        ignored_local_paths = frozenset(
            {
                f"{report.name}/.pbi/localSettings.json",
                f"{semantic_model.name}/.pbi/localSettings.json",
            }
        )
        files = tuple(
            (path.relative_to(project_root).as_posix(), path.read_bytes())
            for path in _project_files(project_root)
            if path.relative_to(project_root).as_posix() not in ignored_local_paths
        )
        if not files:
            raise DesktopHydrationError("Committed PBIP project contains no governed files")
        return ProjectSnapshot(
            project_root=project_root,
            pbip_path=pbip,
            report_path=report,
            semantic_model_path=semantic_model,
            page_ids=page_ids,
            referenced_tables=referenced_tables,
            files=files,
            ignored_local_paths=ignored_local_paths,
        )

    def open_with_bridge(self, pbip_path: Path) -> None:
        """Open the PBIP through Microsoft's registered Desktop bridge launcher."""

        expected = pbip_path.resolve(strict=True)
        desktop = Path(self.desktop_executable)
        if not desktop.is_file():
            raise DesktopHydrationError("Attested Power BI Desktop executable is missing")
        environment = os.environ.copy()
        environment["PBI_DESKTOP_PATH"] = str(desktop)
        payload = self._json_command(
            (
                self.bridge_executable,
                "open",
                str(expected),
                "--timeout",
                "60",
            ),
            "Microsoft Power BI Desktop bridge could not open the committed PBIP",
            environment=environment,
        )
        launched = payload.get("launched")
        report_path = launched.get("reportPath") if isinstance(launched, dict) else None
        pid = payload.get("pid")
        if (
            payload.get("status") != "launched"
            or payload.get("bridgeStatus") != "connected"
            or not isinstance(report_path, str)
            or Path(report_path).resolve(strict=False) != expected
            or not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
        ):
            raise DesktopHydrationError(
                "Microsoft Desktop bridge did not confirm the committed PBIP was opened"
            )

    def hydrate_and_verify(
        self,
        snapshot: ProjectSnapshot,
        evidence_root: str | Path,
    ) -> DesktopHydrationResult:
        """Refresh all tables, prove visual data, and capture every report page."""

        run_root = Path(evidence_root).resolve(strict=False) / uuid.uuid4().hex
        screenshots_root = run_root / "screenshots"
        screenshots_root.mkdir(parents=True, exist_ok=False)
        try:
            desktop_pid = self._desktop_pid(snapshot.pbip_path)
            connection, desktop_process, model_process = self._connect(desktop_pid)
            tables = self._list_tables(connection)
            if not tables:
                raise DesktopHydrationError("The opened semantic model contains no tables")
            for table in tables:
                self._require_processes(desktop_process, model_process)
                self._refresh_table(connection, table)
            self._require_processes(desktop_process, model_process)
            row_counts = tuple(
                (table, self._table_row_count(connection, table))
                for table in snapshot.referenced_tables
            )
            empty_tables = [table for table, count in row_counts if count <= 0]
            if empty_tables:
                names = ", ".join(empty_tables)
                raise DesktopHydrationError(
                    f"Referenced visual tables have no rows after refresh: {names}"
                )
            screenshot_paths = self._capture_pages(
                desktop_pid,
                snapshot.page_ids,
                screenshots_root,
            )
            _assert_project_unchanged(snapshot)
            result = DesktopHydrationResult(
                data_refreshed=True,
                render_verified=True,
                cache_saved=False,
                refreshed_tables=tables,
                screenshot_paths=screenshot_paths,
                evidence_path=str(run_root),
                desktop_pid=desktop_pid,
                referenced_table_rows=row_counts,
            )
            _atomic_write_json(
                run_root / "render-verification.json",
                {
                    "schema_version": "1.0.0",
                    "verified_at": datetime.now(UTC).isoformat(),
                    "pbip_path": str(snapshot.pbip_path),
                    "source_hash": _snapshot_hash(snapshot.files),
                    **result.as_dict(),
                },
            )
            return result
        except Exception as error:
            _best_effort_failure_receipt(run_root, snapshot, error)
            if isinstance(error, DesktopHydrationError):
                raise
            raise DesktopHydrationError(str(error)) from error

    def _desktop_pid(self, pbip_path: Path) -> int:
        expected = os.path.normcase(str(pbip_path.resolve(strict=False)))
        last_reason = "Power BI Desktop bridge did not become ready"
        # Ten four-second bridge waits plus nine one-second backoffs remain
        # below the 60-second outer readiness budget.
        for attempt in range(10):
            try:
                payload = self._json_command(
                    (
                        self.bridge_executable,
                        "status",
                        "--wait-seconds",
                        "4",
                    ),
                    "Power BI Desktop bridge did not become ready",
                )
            except DesktopHydrationError as error:
                if not _is_retryable_bridge_readiness_error(str(error)):
                    raise
                last_reason = str(error)
            else:
                status = payload.get("status")
                instances = payload.get("instances")
                if status in {"not_connected", "error"}:
                    last_reason = "Power BI Desktop bridge host is not ready"
                elif status != "ready" or not isinstance(instances, list):
                    raise DesktopHydrationError("Desktop bridge returned an invalid status receipt")
                else:
                    matches: list[Mapping[str, object]] = []
                    for value in instances:
                        if not isinstance(value, dict) or value.get("bridgeStatus") != "connected":
                            continue
                        current = value.get("currentFilePath")
                        if (
                            isinstance(current, str)
                            and os.path.normcase(str(Path(current).resolve(strict=False)))
                            == expected
                        ):
                            matches.append(value)
                    if len(matches) > 1:
                        raise DesktopHydrationError(
                            "Desktop bridge returned multiple instances for the committed PBIP"
                        )
                    if len(matches) == 1:
                        pid = matches[0].get("pid")
                        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                            raise DesktopHydrationError(
                                "Desktop bridge returned an invalid process id"
                            )
                        return pid
                    last_reason = (
                        "Desktop bridge did not identify the committed PBIP as the active report"
                    )
            if attempt < 9:
                self.sleep(1.0)
        raise DesktopHydrationError(last_reason)

    @staticmethod
    def _require_processes(desktop: psutil.Process, model: psutil.Process) -> None:
        if not desktop.is_running() or not model.is_running():
            raise DesktopHydrationError(
                "The committed PBIP's Power BI Desktop calculation engine closed. "
                "Reopen the project and retry."
            )

    def _connect(self, desktop_pid: int) -> tuple[str, psutil.Process, psutil.Process]:
        """Connect only to the calculation engine of the verified PBIP process."""
        try:
            desktop = psutil.Process(desktop_pid)
            engines = [
                child for child in desktop.children() if child.name().casefold() == "msmdsrv.exe"
            ]
            if len(engines) != 1:
                raise DesktopHydrationError(
                    "The committed PBIP's calculation engine is unavailable or ambiguous. "
                    "Wait for its Power BI Desktop window to load and retry."
                )
            model = engines[0]
            self._require_processes(desktop, model)
            arguments = model.cmdline()
            directory = Path(arguments[arguments.index("-s") + 1])
            port_file = directory / "msmdsrv.port.txt"
            if port_file.stat().st_size > 64:
                raise DesktopHydrationError("The committed PBIP's engine port file is invalid")
            port = port_file.read_text(encoding="utf-16-le").strip("\x00\r\n \ufeff")
            if not port.isascii() or not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise DesktopHydrationError("The committed PBIP's engine port is invalid")
        except (psutil.Error, OSError, ValueError, IndexError) as error:
            raise DesktopHydrationError(
                "The committed PBIP's Power BI Desktop calculation engine is unavailable. "
                "Reopen the project and retry."
            ) from error
        name = f"studio-hydrate-{desktop_pid}-{uuid.uuid4().hex[:12]}"
        payload = self._json_command(
            (
                self.pbi_executable,
                "--json",
                "connect",
                "--data-source",
                f"localhost:{port}",
                "--name",
                name,
            ),
            "pbi-cli could not connect to the opened semantic model",
        )
        connection = payload.get("connection")
        if payload.get("status") != "connected" or connection != name:
            raise DesktopHydrationError(
                "pbi-cli did not confirm the committed PBIP's calculation engine connection"
            )
        self._require_processes(desktop, model)
        return name, desktop, model

    def _list_tables(self, connection: str) -> tuple[str, ...]:
        payload = self._json_command_value(
            (
                self.pbi_executable,
                "--json",
                "--connection",
                connection,
                "table",
                "list",
            ),
            "pbi-cli could not list semantic model tables",
        )
        if not isinstance(payload, list):
            raise DesktopHydrationError("pbi-cli table list did not return an array")
        names: list[str] = []
        for value in payload:
            name = value.get("name") if isinstance(value, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise DesktopHydrationError("pbi-cli table list contained an invalid name")
            names.append(name)
        if len(set(names)) != len(names):
            raise DesktopHydrationError("pbi-cli table list contained duplicate names")
        return tuple(names)

    def _refresh_table(self, connection: str, table: str) -> None:
        payload = self._json_command(
            (
                self.pbi_executable,
                "--json",
                "--connection",
                connection,
                "table",
                "refresh",
                table,
                "--type",
                "Full",
            ),
            f"Full refresh failed for semantic model table {table}",
        )
        if payload.get("status") != "refreshed" or payload.get("name") != table:
            raise DesktopHydrationError(f"pbi-cli did not confirm Full refresh for {table}")

    def _table_row_count(self, connection: str, table: str) -> int:
        escaped = table.replace("'", "''")
        query = f"EVALUATE ROW(\"__row_count\", COUNTROWS('{escaped}'))"
        payload = self._json_command(
            (
                self.pbi_executable,
                "--json",
                "--connection",
                connection,
                "dax",
                "execute",
                "--max-rows",
                "1",
                "--timeout",
                "60",
                query,
            ),
            f"Bounded DAX row probe failed for visual table {table}",
        )
        rows = payload.get("rows")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise DesktopHydrationError(f"DAX row probe returned no result for {table}")
        values = list(rows[0].values())
        if len(values) != 1:
            raise DesktopHydrationError(f"DAX row probe returned an invalid result for {table}")
        value = values[0]
        if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value):
            return int(value)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise DesktopHydrationError(f"DAX row probe returned an invalid count for {table}")

    def _capture_pages(
        self,
        desktop_pid: int,
        expected_page_ids: tuple[str, ...],
        screenshots_root: Path,
    ) -> tuple[str, ...]:
        payload = self._json_command(
            (
                self.bridge_executable,
                "screenshot-all",
                "--pid",
                str(desktop_pid),
                "--output-dir",
                str(screenshots_root),
                "--scale",
                "2",
                "--settle",
                "5000",
                "--wait-seconds",
                "60",
            ),
            "Power BI Desktop could not capture all report pages",
        )
        screenshots = payload.get("screenshots")
        failures = payload.get("failures")
        if (
            payload.get("status") != "ok"
            or not isinstance(screenshots, list)
            or failures not in (None, [])
        ):
            raise DesktopHydrationError("Power BI Desktop did not capture every report page")
        captured_ids: list[str] = []
        paths: list[str] = []
        root = screenshots_root.resolve(strict=True)
        for value in screenshots:
            if not isinstance(value, dict):
                raise DesktopHydrationError("Desktop screenshot receipt is invalid")
            page_id = value.get("pageId")
            output_path = value.get("outputPath")
            if not isinstance(page_id, str) or not isinstance(output_path, str):
                raise DesktopHydrationError("Desktop screenshot receipt is incomplete")
            path = Path(output_path).resolve(strict=True)
            if not path.is_relative_to(root) or path.suffix.casefold() != ".png":
                raise DesktopHydrationError("Desktop screenshot escaped the evidence directory")
            if path.stat().st_size <= 0:
                raise DesktopHydrationError("Desktop produced an empty page screenshot")
            captured_ids.append(page_id)
            paths.append(str(path))
        if tuple(captured_ids) != expected_page_ids:
            raise DesktopHydrationError(
                "Desktop screenshot receipt does not match the PBIR page order"
            )
        return tuple(paths)

    def _json_command(
        self,
        argv: tuple[str, ...],
        failure: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> Mapping[str, object]:
        payload = self._json_command_value(argv, failure, environment=environment)
        if not isinstance(payload, dict):
            raise DesktopHydrationError(f"{failure}: command did not return a JSON object")
        return payload

    def _json_command_value(
        self,
        argv: tuple[str, ...],
        failure: str,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> object:
        try:
            result = self.run_command(argv, self.command_timeout_seconds, environment)
        except (OSError, subprocess.SubprocessError) as error:
            raise DesktopHydrationError(f"{failure}: {error}") from error
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:500]
            suffix = f": {detail}" if detail else ""
            raise DesktopHydrationError(f"{failure}{suffix}")
        try:
            return _extract_final_json(result.stdout)
        except ValueError as error:
            raise DesktopHydrationError(f"{failure}: command returned invalid JSON") from error


def _resolve_report(pbip: Path) -> Path:
    payload = _read_json_object(pbip, "PBIP entry point")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 1:
        raise DesktopHydrationError("PBIP must identify exactly one report artifact")
    artifact = artifacts[0]
    report_value = artifact.get("report") if isinstance(artifact, dict) else None
    relative = report_value.get("path") if isinstance(report_value, dict) else None
    if not isinstance(relative, str):
        raise DesktopHydrationError("PBIP report artifact path is missing")
    relative_path = PurePosixPath(relative.replace("\\", "/"))
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or len(relative_path.parts) != 1
        or relative_path.suffix.casefold() != ".report"
    ):
        raise DesktopHydrationError("PBIP report artifact path is unsafe")
    report = (pbip.parent / relative_path.as_posix()).resolve(strict=True)
    if not report.is_dir() or not report.is_relative_to(pbip.parent):
        raise DesktopHydrationError("PBIP report artifact does not resolve inside the project")
    return report


def _resolve_semantic_model(report: Path, project_root: Path) -> Path:
    payload = _read_json_object(report / "definition.pbir", "report definition")
    reference = payload.get("datasetReference")
    by_path = reference.get("byPath") if isinstance(reference, dict) else None
    relative = by_path.get("path") if isinstance(by_path, dict) else None
    if not isinstance(relative, str):
        raise DesktopHydrationError("Report semantic model path is missing")
    relative_path = Path(relative)
    if relative_path.is_absolute() or relative_path.suffix.casefold() != ".semanticmodel":
        raise DesktopHydrationError("Report semantic model path is unsafe")
    semantic_model = (report / relative_path).resolve(strict=True)
    if not semantic_model.is_dir() or semantic_model.parent != project_root:
        raise DesktopHydrationError("Report semantic model does not resolve inside the project")
    return semantic_model


def _load_page_ids(report: Path) -> tuple[str, ...]:
    payload = _read_json_object(report / "definition/pages/pages.json", "PBIR pages index")
    order = payload.get("pageOrder")
    if (
        not isinstance(order, list)
        or not order
        or not all(isinstance(value, str) and value for value in order)
    ):
        raise DesktopHydrationError("PBIR pages index contains no valid page order")
    page_ids = tuple(order)
    if len(set(page_ids)) != len(page_ids):
        raise DesktopHydrationError("PBIR pages index contains duplicate page ids")
    for page_id in page_ids:
        relative = PurePosixPath(page_id.replace("\\", "/"))
        if relative.is_absolute() or len(relative.parts) != 1 or relative.name != page_id:
            raise DesktopHydrationError("PBIR pages index contains an unsafe page id")
        page = report / "definition/pages" / page_id / "page.json"
        if not page.is_file():
            raise DesktopHydrationError(f"PBIR page definition is missing: {page_id}")
    return page_ids


def _load_referenced_tables(report: Path) -> tuple[str, ...]:
    tables: set[str] = set()
    visual_root = report / "definition/pages"
    for path in sorted(visual_root.glob("*/visuals/*/visual.json")):
        payload = _read_json_object(path, "PBIR visual definition")
        _collect_source_entities(payload, tables)
    return tuple(sorted(tables, key=str.casefold))


def _collect_source_entities(value: object, tables: set[str]) -> None:
    if isinstance(value, dict):
        source = value.get("SourceRef")
        entity = source.get("Entity") if isinstance(source, dict) else None
        if isinstance(entity, str) and entity:
            tables.add(entity)
        for child in value.values():
            _collect_source_entities(child, tables)
    elif isinstance(value, list):
        for child in value:
            _collect_source_entities(child, tables)


def _project_files(project_root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for path in sorted(project_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)()):
            raise DesktopHydrationError(f"PBIP project contains a link-like path: {path}")
        if path.is_file():
            files.append(path)
        elif not path.is_dir():
            raise DesktopHydrationError(f"PBIP project contains an unsupported path: {path}")
    return tuple(files)


def _assert_project_unchanged(snapshot: ProjectSnapshot) -> None:
    expected = dict(snapshot.files)
    actual: dict[str, bytes] = {}
    for path in _project_files(snapshot.project_root):
        relative = path.relative_to(snapshot.project_root).as_posix()
        if relative not in snapshot.ignored_local_paths:
            actual[relative] = path.read_bytes()
    changed = sorted(
        path for path in expected.keys() | actual.keys() if expected.get(path) != actual.get(path)
    )
    if changed:
        preview = ", ".join(changed[:5])
        raise DesktopHydrationError(
            f"Desktop changed committed PBIP source files during hydration: {preview}"
        )


def _extract_final_json(output: str) -> object:
    """Decode the final JSON envelope after third-party CLI progress lines."""

    decoder = json.JSONDecoder()
    starts = [index for index, character in enumerate(output) if character in "{["]
    for start in reversed(starts):
        try:
            payload, end = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            continue
        if not output[end:].strip():
            return payload
    raise ValueError("no final JSON envelope")


def _is_retryable_bridge_readiness_error(message: str) -> bool:
    normalized = message.casefold().replace("_", " ").replace("-", " ")
    return any(
        token in normalized
        for token in (
            "host not ready",
            "not connected",
            "bridge unavailable",
        )
    )


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DesktopHydrationError(f"{label} is missing or invalid JSON") from error
    if not isinstance(payload, dict):
        raise DesktopHydrationError(f"{label} must contain a JSON object")
    return payload


def _snapshot_hash(files: tuple[tuple[str, bytes], ...]) -> str:
    digest = hashlib.sha256()
    for relative, content in files:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _best_effort_failure_receipt(
    run_root: Path,
    snapshot: ProjectSnapshot,
    error: Exception,
) -> None:
    try:
        _atomic_write_json(
            run_root / "render-verification.json",
            {
                "schema_version": "1.0.0",
                "verified_at": datetime.now(UTC).isoformat(),
                "pbip_path": str(snapshot.pbip_path),
                "source_hash": _snapshot_hash(snapshot.files),
                "data_refreshed": False,
                "render_verified": False,
                "cache_saved": False,
                "error": str(error),
            },
        )
    except OSError:
        return
